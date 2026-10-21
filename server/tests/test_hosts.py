"""Maintenance hosts (offline, agregat, partisi) dan halaman Komputer/Detail."""
import re
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.auth.audit import SYSTEM
from app.core.database import async_session_factory, engine
from app.folders import service as folder_service
from app.hosts import tasks as host_tasks
from tests.conftest import bearer, heartbeat_payload, htmx_headers, login, make_user, online_host, sql


async def folder(name: str, parent: int | None = None, subnet: str | None = None) -> int:
    async with async_session_factory() as db:
        f = await folder_service.create_folder(db, name=name, parent_id=parent, location=None, subnet_hint=subnet,
                                               actor=SYSTEM)
    return f.id


async def test_folder_depth_and_suggestion():
    unit = await folder("IGD")
    room = await folder("Triase", unit, "10.10.12.0/24")
    async with async_session_factory() as db:
        try:
            await folder_service.create_folder(db, name="Terlalu dalam", parent_id=room, location=None,
                                               subnet_hint=None, actor=SYSTEM)
            raise AssertionError("tingkat 3 harus ditolak")
        except folder_service.FolderError:
            pass
        async with db.begin():
            tree = await folder_service.load_tree(db)
    assert folder_service.suggest(tree, "10.10.12.77").path == "IGD › Triase"
    assert folder_service.suggest(tree, "10.10.99.1") is None


async def test_mark_offline_after_threshold(client):
    host_id, _ = await online_host(client)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE hosts SET last_seen_at = NOW() - INTERVAL 10 MINUTE WHERE id = :h"),
                           {"h": host_id})
    await host_tasks.mark_offline({"db": async_session_factory})
    assert (await sql("SELECT status FROM hosts WHERE id = :h", h=host_id))[0][0] == "offline"
    assert await sql("SELECT id FROM host_events WHERE message = 'Offline (tidak ada heartbeat)'")


async def test_hourly_aggregation_with_watermark(client):
    host_id, token = await online_host(client)
    async with engine.begin() as conn:
        hour = (await conn.execute(text(
            "SELECT DATE_FORMAT(UTC_TIMESTAMP() - INTERVAL 2 HOUR, '%Y-%m-%d %H:00:00')"))).scalar()
        await conn.execute(text(
            "INSERT INTO metrics (host_id, ts, cpu_pct, ram_pct) VALUES "
            "(:h, :t, 10, 50), (:h, :t + INTERVAL 1 MINUTE, 30, 70), (:h, :t + INTERVAL 30 SECOND, 20, 60)"),
            {"h": host_id, "t": hour})
        previous = datetime.fromisoformat(hour) - timedelta(hours=1)        # watermark = jam sebelumnya
        await conn.execute(text("INSERT INTO settings (name, value) VALUES ('metrics_hourly_done', :v)"),
                           {"v": f'"{previous.isoformat()}"'})
    await host_tasks.aggregate_hourly({"db": async_session_factory})
    row = (await sql("SELECT cpu_avg, cpu_max, online_min FROM metrics_hourly WHERE host_id = :h", h=host_id))[0]
    assert (float(row[0]), float(row[1]), row[2]) == (20.0, 30.0, 2)   # 2 menit berbeda ada sampel


async def test_partitions_created_ahead_and_dropped_by_retention():
    async with engine.begin() as conn:
        today = (await conn.execute(text("SELECT UTC_DATE()"))).scalar()
        # Partisi lama tiruan (40 hari lalu) di depan partisi yang dibuat migrasi
        names = [r[0] for r in await conn.execute(text(
            "SELECT PARTITION_NAME FROM information_schema.PARTITIONS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'metrics' ORDER BY PARTITION_ORDINAL_POSITION"))]
        first = names[0]
        old = date.fromordinal(today.toordinal() - 40)
        await conn.execute(text(
            f"ALTER TABLE metrics REORGANIZE PARTITION {first} INTO ("
            f"PARTITION p{old:%Y%m%d} VALUES LESS THAN ('{date.fromordinal(old.toordinal() + 1)}'), "
            f"PARTITION {first} VALUES LESS THAN ('{host_tasks.partition_bound(first)}'))"))
    await host_tasks.maintain_partitions(async_session_factory, force=True)
    names = [r[0] for r in await sql(
        "SELECT PARTITION_NAME FROM information_schema.PARTITIONS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'metrics' ORDER BY PARTITION_ORDINAL_POSITION")]
    assert f"p{old:%Y%m%d}" not in names                                           # retensi 30 hari
    ahead = date.fromordinal(today.toordinal() + host_tasks.DAYS_AHEAD)
    assert f"p{ahead:%Y%m%d}" in names and names[-1] == "pmax"


async def test_komputer_page_groups_and_move(client):
    unit = await folder("IGD")
    room = await folder("Triase", unit, "10.10.12.0/24")
    host_id, _ = await online_host(client, "IGD-PC01")
    await make_user("tek", role="teknisi")
    csrf = await login(client, "tek")
    page = await client.get("/hosts")
    assert page.status_code == 200 and "Belum dikelompokkan" in page.text and "/hosts/folders" not in page.text
    assert 'name="host_id"' not in (await client.get("/hosts?group=none")).text      # Teknisi: tanpa checkbox pindah
    move = {"host_id": host_id, "folder_id": room}
    assert (await client.post("/hosts/move", data=move, headers=htmx_headers(csrf))).status_code == 403
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    resp = await client.post("/hosts/move", data=move, headers=htmx_headers(csrf))
    assert resp.status_code == 204 and resp.headers["HX-Refresh"] == "true"
    rows = await client.get(f"/hosts/rows?unit={unit}", headers=htmx_headers())
    assert "IGD-PC01" in rows.text and "IGD › Triase" in rows.text and "<html" not in rows.text
    assert (await sql("SELECT action, detail FROM audit_logs WHERE action = 'Pindah folder'"))[0][1] == (
        "Belum dikelompokkan → IGD › Triase")
    search = await client.get("/hosts?q=PC01")
    assert "IGD-PC01" in search.text


async def test_komputer_group_remembered_and_folders_open(client):
    await folder("IGD")
    await make_user("tek", role="teknisi")
    await login(client, "tek")
    page = await client.get("/hosts")
    assert "dikelompokkan per folder" in page.text and '<details class="grp" open>' in page.text  # default terbuka
    await client.get("/hosts?group=status")
    assert "dikelompokkan per status" in (await client.get("/hosts")).text          # diingat lewat cookie
    assert "dikelompokkan per folder" in (await client.get("/hosts?group=folder")).text
    assert "dikelompokkan per folder" in (await client.get("/hosts")).text


async def test_edit_specs_in_actions_tab_survives_inventory(client):
    host_id, token = await online_host(client, "IGD-PC01")
    await client.put("/api/agent/inventory", headers=bearer(token),
                     json={"manufacturer": "Lenovo", "model": "M70s", "serial_number": "AGENT-SN"})
    await make_user("tek", role="teknisi")
    csrf = await login(client, "tek")
    page = await client.get(f"/hosts/{host_id}?tab=actions")
    assert "Spesifikasi" in page.text and "Ubah…" not in page.text and "Refresh inventaris" not in page.text
    for path in ("specs", "inventory"):
        assert (await client.post(f"/hosts/{host_id}/{path}", data={"hostname": "X"},
                                  headers=htmx_headers(csrf))).status_code == 403
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    page = await client.get(f"/hosts/{host_id}?tab=actions")
    assert 'id="spec-dialog"' in page.text and "Ubah…" in page.text
    assert "Ubah…" not in (await client.get(f"/hosts/{host_id}?tab=summary")).text

    bad = await client.post(f"/hosts/{host_id}/specs", data={"hostname": "PC 01"}, headers=htmx_headers(csrf))
    assert bad.status_code == 422
    form = {"hostname": "IGD-PC01-BARU", "manufacturer": "Lenovo", "model": "ThinkCentre M70s",
            "serial_number": "SN-MANUAL", "inventory_no": "INV/IGD/001"}
    resp = await client.post(f"/hosts/{host_id}/specs", data=form, headers=htmx_headers(csrf))
    assert resp.status_code == 204 and resp.headers["HX-Refresh"] == "true"
    assert (await sql("SELECT action, target_label FROM audit_logs WHERE action = 'Ubah data PC'"))[0][1] == "IGD-PC01"

    # Inventaris berikutnya tidak menimpa field yang diubah manual; field yang tidak diubah tetap ikut agent
    await client.put("/api/agent/inventory", headers=bearer(token),
                     json={"manufacturer": "LENOVO", "model": "11DT", "serial_number": "AGENT-SN2"})
    row = (await sql("SELECT hostname, manufacturer, model, serial_number, inventory_no FROM hosts WHERE id = :h",
                     h=host_id))[0]
    assert tuple(row) == ("IGD-PC01-BARU", "LENOVO", "ThinkCentre M70s", "SN-MANUAL", "INV/IGD/001")

    # Dikosongkan = kembali memakai data agent
    form["serial_number"] = ""
    await client.post(f"/hosts/{host_id}/specs", data=form, headers=htmx_headers(csrf))
    await client.put("/api/agent/inventory", headers=bearer(token), json={"serial_number": "AGENT-SN3"})
    assert (await sql("SELECT serial_number FROM hosts WHERE id = :h", h=host_id))[0][0] == "AGENT-SN3"


async def test_detail_tabs_full_page_and_fragment(client):
    host_id, token = await online_host(client)
    await client.put("/api/agent/apps", headers=bearer(token),
                     json={"hash": "c" * 64, "apps": [{"name": "SIMRS Client", "version": "4.2.1"}]})
    await make_user("tek", role="teknisi")
    await login(client, "tek")
    for tab in ("summary", "apps", "network", "history", "screenshot", "remote", "actions"):
        full = await client.get(f"/hosts/{host_id}?tab={tab}")
        assert full.status_code == 200 and "<html" in full.text
        frag = await client.get(f"/hosts/{host_id}?tab={tab}", headers=htmx_headers())
        assert "<html" not in frag.text and 'class="tabs-line"' in frag.text
        assert frag.headers["vary"] == "HX-Request"
    apps = await client.get(f"/hosts/{host_id}/apps?q=simrs", headers=htmx_headers())
    assert "SIMRS Client" in apps.text
    assert "Ambil screenshot" in (await client.get(f"/hosts/{host_id}?tab=screenshot")).text
    for r in ("1h", "7d", "30d"):
        assert (await client.get(f"/hosts/{host_id}/charts?range={r}")).status_code == 200
    assert (await client.get("/hosts/999999")).status_code == 404


async def test_pending_host_not_visible_in_detail(client):
    from tests.conftest import enroll, enrollment_key
    data = await enroll(client, await enrollment_key())
    await make_user("tek", role="teknisi")
    await login(client, "tek")
    assert (await client.get(f"/hosts/{data['host_id']}")).status_code == 404
    assert (await client.get(f"/hosts/{data['host_id']}/events?before=999999")).status_code == 404
    assert re.search(r"Komputer", (await client.get("/hosts")).text)
    hb = await client.post("/api/agent/heartbeat", headers=bearer(data["agent_token"]), json=heartbeat_payload())
    assert hb.json()["status"] == "pending"
