"""Evaluasi aturan alert (worker), ringkasan masalah di hosts, aksi alert, notifikasi."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.alerts import tasks as alert_tasks
from app.alerts.evaluate import evaluate_all
from app.alerts.models import AlertRule, AlertRuleChannel, NotificationChannel
from app.core.database import async_session_factory
from app.jobs.models import Job
from tests.conftest import (
    bearer, heartbeat_payload, htmx_headers, login, make_user, online_host, sql, sql_exec,
)


async def add_rule(metric="disk_pct", op=">=", threshold=90, duration=0, level="critical", channel=None,
                   folder_id=None):
    async with async_session_factory() as db, db.begin():
        rule = AlertRule(name=f"Aturan {metric}", metric=metric, operator=op, threshold=threshold,
                         duration_sec=duration, level=level, folder_id=folder_id)
        db.add(rule)
        await db.flush()
        if channel:
            ch = NotificationChannel(kind="telegram", name="Grup IT", target="-100123")
            db.add(ch)
            await db.flush()
            db.add(AlertRuleChannel(rule_id=rule.id, channel_id=ch.id))
        return rule.id


async def test_disk_rule_opens_once_and_resolves(client):
    rule_id = await add_rule(channel=True)
    host_id, token = await online_host(client, disks=[{"mount": "C:", "used_pct": 94}])
    await evaluate_all(async_session_factory)
    await evaluate_all(async_session_factory)                       # putaran kedua tidak menggandakan
    alerts = await sql("SELECT message, level, status FROM alerts WHERE rule_id = :r", r=rule_id)
    assert alerts == [("Disk C: 94% (ambang 90%)", "critical", "new")]
    assert (await sql("SELECT problem_level, problem_text FROM hosts WHERE id = :h", h=host_id))[0] == (
        "critical", "Disk C: 94%")
    async with async_session_factory() as db:
        jobs = list(await db.scalars(select(Job)))
    assert [(j.task, j.payload["event"]) for j in jobs] == [("alerts.notify", "open")]   # outbox, satu per kanal

    await client.post("/api/agent/heartbeat", headers=bearer(token),
                      json=heartbeat_payload(disks=[{"mount": "C:", "used_pct": 60}]))
    await evaluate_all(async_session_factory)
    row = (await sql("SELECT status, resolve_note, open_flag FROM alerts WHERE rule_id = :r", r=rule_id))[0]
    assert row == ("resolved", "Otomatis (kembali normal)", None)
    assert (await sql("SELECT problem_level, problem_count FROM hosts WHERE id = :h", h=host_id))[0] == (None, 0)
    assert await sql("SELECT id FROM host_events WHERE message LIKE 'Alert selesai:%'")


async def test_windowed_cpu_rule_needs_whole_window(client):
    await add_rule(metric="cpu_pct", op=">", threshold=95, duration=600, level="warning")
    host_id, token = await online_host(client, cpu_pct=99)
    now = datetime.now(UTC).replace(microsecond=0)

    async def samples(values):
        payload = [{"ts": (now - timedelta(minutes=i + 1)).isoformat(), "cpu_pct": v, "ram_pct": 50}
                   for i, v in enumerate(values)]
        await client.post("/api/agent/metrics", headers=bearer(token), json={"samples": payload})

    await samples([99, 98, 40, 99])                                  # satu sampel normal dalam jendela
    await evaluate_all(async_session_factory)
    assert await sql("SELECT id FROM alerts") == []
    await samples([99, 98, 97, 99, 99, 98, 99, 99, 97, 98])          # semua di atas ambang 10 menit
    await evaluate_all(async_session_factory)
    assert (await sql("SELECT message FROM alerts"))[0][0] == "CPU 99% selama 10 mnt (ambang 95%)"


async def test_offline_rule_uses_last_seen(client):
    await add_rule(metric="offline_min", op=">", threshold=10, level="critical")
    host_id, _ = await online_host(client)
    await sql("SELECT 1")
    async with async_session_factory() as db, db.begin():
        from sqlalchemy import text
        await db.execute(text("UPDATE hosts SET last_seen_at = NOW() - INTERVAL 15 MINUTE WHERE id = :h"),
                         {"h": host_id})
    await evaluate_all(async_session_factory)
    assert (await sql("SELECT problem_text FROM hosts WHERE id = :h", h=host_id))[0][0] == "Offline 15 mnt"


async def test_alert_actions_permissions_and_audit(client):
    rule_id = await add_rule()
    await online_host(client, disks=[{"mount": "C:", "used_pct": 95}])
    await evaluate_all(async_session_factory)
    alert_id = (await sql("SELECT id FROM alerts"))[0][0]

    await make_user("tek", role="teknisi")
    csrf = await login(client, "tek")
    assert (await client.post("/alerts/ack", data={"alert_id": alert_id})).status_code == 403   # tanpa CSRF
    resp = await client.post("/alerts/ack", data={"alert_id": alert_id, "note": "menuju lokasi"},
                             headers=htmx_headers(csrf))
    assert resp.status_code == 200 and "1 alert diakui" in resp.headers["HX-Trigger"]
    # Teknisi tidak boleh mengubah aturan
    assert (await client.post(f"/alerts/rules/{rule_id}/toggle", data={}, headers=htmx_headers(csrf))
            ).status_code == 403
    resp = await client.post("/alerts/resolve", data={"alert_id": alert_id, "note": "disk dibersihkan"},
                             headers=htmx_headers(csrf))
    assert resp.status_code == 200
    row = (await sql("SELECT status, resolve_note FROM alerts"))[0]
    assert row == ("resolved", "tek · disk dibersihkan")
    actions = [r[0] for r in await sql("SELECT action FROM audit_logs WHERE category = 'alert' ORDER BY id")]
    assert actions == ["Akui alert", "Selesaikan alert"]
    assert (await sql("SELECT note FROM alert_notes"))[0][0] == "menuju lokasi"


async def test_disabling_rule_resolves_open_alerts(client):
    rule_id = await add_rule()
    host_id, _ = await online_host(client, disks=[{"mount": "C:", "used_pct": 95}])
    await evaluate_all(async_session_factory)
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    resp = await client.post(f"/alerts/rules/{rule_id}/toggle", data={}, headers=htmx_headers(csrf))
    assert resp.status_code == 200
    assert (await sql("SELECT resolve_note FROM alerts"))[0][0] == "Aturan dinonaktifkan"
    assert (await sql("SELECT problem_level FROM hosts WHERE id = :h", h=host_id))[0][0] is None


@pytest.mark.parametrize(("level", "local_hour", "sent"), [
    ("warning", 12, True), ("warning", 23, False), ("critical", 23, True),
])
async def test_notify_respects_quiet_hours(client, monkeypatch, level, local_hour, sent):
    from app.alerts import service as alert_service
    from app.core.config import settings
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(alert_service, "local_now", lambda: datetime(2026, 10, 14, local_hour, 0))
    await add_rule(level=level, channel=True)
    await online_host(client, disks=[{"mount": "C:", "used_pct": 95}])
    await evaluate_all(async_session_factory)
    job = (await sql("SELECT payload FROM jobs"))[0][0]
    import json
    payload = json.loads(job)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await alert_tasks.notify({"db": async_session_factory, "http": http}, **payload)
    assert bool(requests) is sent
    if sent:
        body = json.loads(requests[0].content)
        assert body["chat_id"] == "-100123" and "Disk C: 95%" in body["text"]
        # Per alert_id job, bukan baris pertama: test pertama setelah migrasi masih membawa aturan bawaan migrasi
        assert (await sql("SELECT notified_at IS NOT NULL FROM alerts WHERE id = :a", a=payload["alert_id"]))[0][0] == 1


async def test_unchanged_value_is_not_rewritten(client):
    """Nilai berdesimal (94.37) dibandingkan dengan DECIMAL(10,2) tersimpan: putaran berikut tanpa UPDATE apa pun
    (dulu float != Decimal selalu benar → hosts.problem_* ditulis ulang tiap putaran)."""
    from sqlalchemy import event

    from app.core.database import engine
    await add_rule()
    await online_host(client, disks=[{"mount": "C:", "used_pct": 94.37}])
    await evaluate_all(async_session_factory)
    updates = []

    def count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        await evaluate_all(async_session_factory)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert updates == []
    assert str((await sql("SELECT value FROM alerts"))[0][0]) == "94.37"



ORPHAN_NOTE = "Otomatis (PC dicabut / di luar cakupan aturan)"


async def test_alert_of_revoked_host_is_resolved(client):
    from app.auth.audit import SYSTEM
    from app.hosts import service as host_service
    rule_id = await add_rule()
    host_id, _ = await online_host(client, disks=[{"mount": "C:", "used_pct": 95}])
    await evaluate_all(async_session_factory)
    async with async_session_factory() as db:
        await host_service.revoke_token(db, host_id, SYSTEM)
    await evaluate_all(async_session_factory)
    assert await sql("SELECT status, resolve_note FROM alerts WHERE rule_id = :r", r=rule_id) == [
        ("resolved", ORPHAN_NOTE)]
    assert (await sql("SELECT problem_level, problem_count FROM hosts WHERE id = :h", h=host_id))[0] == (None, 0)


async def test_alert_of_host_moved_out_of_rule_folder_is_resolved(client):
    from tests.test_hosts import folder
    unit, other = await folder("IGD"), await folder("Farmasi")
    rule_id = await add_rule(folder_id=unit)
    host_id, _ = await online_host(client, folder_id=unit, disks=[{"mount": "C:", "used_pct": 95}])
    await evaluate_all(async_session_factory)
    assert (await sql("SELECT status FROM alerts WHERE rule_id = :r", r=rule_id))[0][0] == "new"
    await sql_exec("UPDATE hosts SET folder_id = :f WHERE id = :h", f=other, h=host_id)
    await evaluate_all(async_session_factory)
    assert (await sql("SELECT resolve_note FROM alerts WHERE rule_id = :r", r=rule_id))[0][0] == ORPHAN_NOTE


async def test_alert_of_offline_host_stays_open(client):
    """PC offline masih dalam cakupan: alert disk-nya dibiarkan (datanya basi), bukan diselesaikan."""
    rule_id = await add_rule()
    host_id, _ = await online_host(client, disks=[{"mount": "C:", "used_pct": 95}])
    await evaluate_all(async_session_factory)
    await sql_exec("UPDATE hosts SET last_seen_at = NOW() - INTERVAL 1 HOUR, status = 'offline' WHERE id = :h",
                   h=host_id)
    await evaluate_all(async_session_factory)
    assert (await sql("SELECT status FROM alerts WHERE rule_id = :r", r=rule_id))[0][0] == "new"


@pytest.mark.parametrize("bad", ["220000", "T22:00", "2200", "22", "24:00", "22:60", "22:00Z", ""])
async def test_quiet_hours_rejects_formats_that_would_break_notify(client, bad):
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    before = await sql("SELECT value FROM settings WHERE name = 'quiet_hours'")
    for data in ({"start": bad, "end": "06:00"}, {"start": "22:00", "end": bad}):
        assert (await client.post("/alerts/quiet-hours", data=data, headers=htmx_headers(csrf))).status_code == 422
    assert await sql("SELECT value FROM settings WHERE name = 'quiet_hours'") == before     # tidak tersimpan


async def test_quiet_hours_saved_value_is_usable(client):
    import json

    from app.alerts import service as alert_service
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    resp = await client.post("/alerts/quiet-hours", data={"start": "22:30", "end": "06:00:00"},
                             headers=htmx_headers(csrf))
    assert resp.status_code == 200
    value = json.loads((await sql("SELECT value FROM settings WHERE name = 'quiet_hours'"))[0][0])
    assert (value["start"], value["end"]) == ("22:30", "06:00")
    assert alert_service.in_quiet_hours(value, datetime(2026, 10, 14, 23, 0))
