"""API agent: enrollment, persetujuan, heartbeat, metrik batch, inventaris, aplikasi, screenshot."""
from datetime import UTC, datetime, timedelta

from tests.conftest import (
    bearer, enroll, enrollment_key, heartbeat_payload, htmx_headers, login, make_user, online_host, sql,
)


async def test_enroll_requires_valid_key_and_starts_pending(client):
    assert (await client.post("/api/agent/enroll", json={"enrollment_key": "SALAH-1234-ABCD", "hostname": "X"})
            ).status_code == 403
    key = await enrollment_key()
    data = await enroll(client, key)
    assert data["status"] == "pending"
    # Token disimpan sebagai hash, bukan nilai mentah
    rows = await sql("SELECT status, agent_token_hash FROM hosts WHERE id = :i", i=data["host_id"])
    assert rows[0][0] == "pending" and data["agent_token"] not in rows[0][1]

    # Pending: heartbeat diterima (tanda hidup) tapi data lain ditolak sampai disetujui admin
    resp = await client.post("/api/agent/heartbeat", headers=bearer(data["agent_token"]), json=heartbeat_payload())
    assert resp.json() == {"status": "pending", "commands": []}
    assert (await client.post("/api/agent/metrics", headers=bearer(data["agent_token"]), json={})).status_code == 403
    assert await sql("SELECT * FROM host_latest") == []


async def test_revoked_or_wrong_token_is_rejected(client):
    host_id, token = await online_host(client)
    assert (await client.post("/api/agent/heartbeat", headers=bearer("palsu"), json=heartbeat_payload())
            ).status_code == 401
    await make_user("admin1", role="admin")
    csrf = await login(client, "admin1")
    resp = await client.post(f"/hosts/{host_id}/revoke", headers=htmx_headers(csrf))
    assert resp.status_code == 204 and resp.headers["HX-Redirect"] == "/hosts"
    assert (await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload())
            ).status_code == 401
    assert (await sql("SELECT action, is_sensitive FROM audit_logs WHERE action = 'Cabut token agent'"))[0][1] == 1


async def test_heartbeat_updates_latest_and_events(client):
    host_id, token = await online_host(client, cpu_pct=12, disks=[{"mount": "C:", "used_pct": 94},
                                                                    {"mount": "D:", "used_pct": 41}])
    latest = (await sql("SELECT cpu_pct, disk_max_pct, disk_max_mount, logged_user FROM host_latest"))[0]
    assert (float(latest[0]), float(latest[1]), latest[2], latest[3]) == (12, 94, "C:", "perawat.igd2")
    kinds = [r[0] for r in await sql("SELECT kind FROM host_events WHERE host_id = :h ORDER BY id", h=host_id)]
    assert "login" in kinds and kinds.count("agent") >= 2       # daftar, disetujui, PC menyala

    # Heartbeat berikutnya dengan user & boot sama tidak menambah kejadian
    before = len(kinds)
    await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload())
    assert len(await sql("SELECT id FROM host_events WHERE host_id = :h", h=host_id)) == before

    # Versi agent berubah → tercatat di Riwayat
    await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload(agent_version="1.3.1"))
    assert await sql("SELECT id FROM host_events WHERE message LIKE 'Agent diperbarui 1.3.0 → 1.3.1'")


async def test_metrics_batch_upsert_and_clock_filter(client):
    host_id, token = await online_host(client)
    now = datetime.now(UTC).replace(microsecond=0)
    samples = [{"ts": (now - timedelta(minutes=m)).isoformat(), "cpu_pct": m, "ram_pct": 50} for m in range(1, 11)]
    samples.append({"ts": (now - timedelta(days=5)).isoformat(), "cpu_pct": 1, "ram_pct": 1})     # jam PC ngawur
    samples.append({"ts": (now + timedelta(hours=2)).isoformat(), "cpu_pct": 1, "ram_pct": 1})
    resp = await client.post("/api/agent/metrics", headers=bearer(token), json={
        "samples": samples, "disks": [{"ts": now.isoformat(), "mount": "C:", "used_pct": 70}]})
    assert resp.json() == {"stored": 11}
    # Kirim ulang dari buffer agent: menimpa, bukan error duplikat
    resp = await client.post("/api/agent/metrics", headers=bearer(token), json={"samples": samples[:3]})
    assert resp.status_code == 200
    assert (await sql("SELECT COUNT(*) FROM metrics WHERE host_id = :h", h=host_id))[0][0] == 10
    # Validasi: nilai di luar 0–100 → 422 JSON (bukan 500)
    bad = await client.post("/api/agent/metrics", headers=bearer(token),
                            json={"samples": [{"ts": now.isoformat(), "cpu_pct": 120, "ram_pct": 1}]})
    assert bad.status_code == 422 and "detail" in bad.json()


async def test_inventory_and_apps_replace_and_log_changes(client):
    host_id, token = await online_host(client)
    inv = {"os_name": "Windows 10 Pro 22H2", "ram_total_mb": 8192, "model": "ThinkCentre M70s",
           "disks": [{"mount": "C:", "total_bytes": 256 * 10**9, "used_bytes": 200 * 10**9, "media": "SSD"}],
           "adapters": [{"name": "Ethernet", "is_up": True, "is_server_route": True, "ipv4": "10.10.12.43"},
                        {"name": "Wi-Fi", "is_up": False}]}
    assert (await client.put("/api/agent/inventory", headers=bearer(token), json=inv)).status_code == 200
    inv["adapters"] = inv["adapters"][:1]
    await client.put("/api/agent/inventory", headers=bearer(token), json=inv)
    assert [r[0] for r in await sql("SELECT name FROM network_adapters WHERE host_id = :h", h=host_id)] == ["Ethernet"]
    assert (await sql("SELECT ram_total_mb FROM hosts WHERE id = :h", h=host_id))[0][0] == 8192

    apps = [{"name": "Google Chrome", "version": "128"}, {"name": "7-Zip", "version": "23.01"}]
    assert (await client.put("/api/agent/apps", headers=bearer(token), json={"hash": "a" * 64, "apps": apps})
            ).json() == {"changed": True}
    assert (await client.put("/api/agent/apps", headers=bearer(token), json={"hash": "a" * 64, "apps": apps})
            ).json() == {"changed": False}
    apps = [{"name": "Google Chrome", "version": "129"}, {"name": "AnyDesk", "version": "8.1"}]
    await client.put("/api/agent/apps", headers=bearer(token), json={"hash": "b" * 64, "apps": apps})
    messages = {r[0] for r in await sql("SELECT message FROM host_events WHERE kind = 'app'")}
    assert {"Aplikasi berubah: Google Chrome 128 → 129", "Aplikasi dipasang: AnyDesk 8.1",
            "Aplikasi dihapus: 7-Zip"} <= messages
    assert sorted(r[0] for r in await sql("SELECT name FROM installed_apps")) == ["AnyDesk", "Google Chrome"]


async def test_screenshot_flow_admin_only_and_audited(client, tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "SCREENSHOT_DIR", tmp_path)
    host_id, token = await online_host(client)
    await make_user("tek", role="teknisi")
    csrf = await login(client, "tek")
    resp = await client.post(f"/hosts/{host_id}/screenshots", data={"reason": "tiket #1"}, headers=htmx_headers(csrf))
    assert resp.status_code == 403                                     # teknisi tidak boleh screenshot

    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    resp = await client.post(f"/hosts/{host_id}/screenshots", data={"reason": "x"}, headers=htmx_headers(csrf))
    assert resp.status_code == 422 and "Alasan wajib" in resp.text      # alasan wajib
    resp = await client.post(f"/hosts/{host_id}/screenshots", data={"reason": "tiket #2318", "notify_user": "1"},
                             headers=htmx_headers(csrf))
    assert resp.status_code == 200
    commands = (await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload())
                ).json()["commands"]
    request_id = commands[0]["request_id"]
    assert commands == [{"type": "screenshot", "request_id": request_id, "notify": True}]
    bad = await client.post(f"/api/agent/screenshots/{request_id}", headers=bearer(token), content=b"bukan gambar")
    assert bad.status_code == 422
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    ok = await client.post(f"/api/agent/screenshots/{request_id}", headers={**bearer(token), "X-Width": "1600",
                                                                           "X-Height": "900"}, content=jpeg)
    assert ok.status_code == 200
    shot_id, status, path = (await sql("SELECT id, status, file_path FROM screenshots"))[0]
    assert status == "uploaded" and (tmp_path / path).read_bytes() == jpeg
    image = await client.get(f"/hosts/{host_id}/screenshots/{shot_id}")
    assert image.content == jpeg and image.headers["cache-control"] == "no-store"
    actions = [r[0] for r in await sql("SELECT action FROM audit_logs WHERE category = 'screenshot' ORDER BY id")]
    assert actions == ["Ambil screenshot", "Lihat screenshot"]

    # kotak "Beri notifikasi ke user" tidak dicentang → agent diminta tidak menampilkan pemberitahuan
    resp = await client.post(f"/hosts/{host_id}/screenshots", data={"reason": "tiket #2319"}, headers=htmx_headers(csrf))
    assert resp.status_code == 200
    commands = (await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload())
                ).json()["commands"]
    assert [c["notify"] for c in commands] == [False]
