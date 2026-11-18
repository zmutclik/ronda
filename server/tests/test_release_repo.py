"""Rilis agent dari agent/releases di repo (git pull) dan naik ring otomatis oleh worker."""
import hashlib
import json

import pytest

from app.agents import service as agent_service
from app.core.config import settings
from app.core.database import async_session_factory
from tests.conftest import bearer, heartbeat_payload, login, make_user, online_host, sql, sql_exec


def put_release(directory, version: str, content: bytes | None = None, **override) -> bytes:
    content = content if content is not None else f"paket {version}".encode() * 100
    (directory / f"ronda-agent-{version}.zip").write_bytes(content)
    meta = {"version": version, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "signature": "A" * 88, "notes": f"rilis {version}"} | override
    (directory / f"ronda-agent-{version}.json").write_text(json.dumps(meta))
    return content


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    repo, store = tmp_path / "repo", tmp_path / "store"
    repo.mkdir()
    monkeypatch.setattr(settings, "RELEASE_DIR", store)
    monkeypatch.setattr(agent_service, "_bad_repo_files", {})
    return repo, store


async def sync(repo):
    async with async_session_factory() as db:
        return await agent_service.sync_repo_releases(db, repo)


async def promote():
    async with async_session_factory() as db:
        return await agent_service.auto_promote(db)


async def test_sync_registers_and_starts_newest(dirs):
    repo, store = dirs
    put_release(repo, "1.2.0")
    content = put_release(repo, "1.10.0")                 # urutan versi numerik, bukan teks
    assert sorted(await sync(repo)) == ["1.10.0", "1.2.0"]
    rows = await sql("SELECT version, status, current_ring, uploaded_by, notes FROM agent_releases ORDER BY version")
    assert rows == [("1.10.0", "rolling", 0, None, "rilis 1.10.0"), ("1.2.0", "uploaded", 0, None, "rilis 1.2.0")]
    assert (store / "ronda-agent-1.10.0.zip").read_bytes() == content
    assert sorted(p.name for p in store.iterdir()) == ["ronda-agent-1.10.0.zip", "ronda-agent-1.2.0.zip"]
    actions = [r[0] for r in await sql("SELECT action FROM audit_logs WHERE username = 'sistem' ORDER BY id")]
    assert actions == ["Rilis dari repo", "Rilis dari repo", "Mulai rilis"]

    assert await sync(repo) == []                          # sudah terdaftar: tidak didaftarkan/dimulai ulang


async def test_sync_does_not_restart_stopped_or_start_older(dirs):
    repo, _ = dirs
    put_release(repo, "2.0.0")
    await sync(repo)
    await sql_exec("UPDATE agent_releases SET status = 'stopped'")
    put_release(repo, "1.9.0")                             # lebih lama dari rilis yang ada: hanya didaftarkan
    assert await sync(repo) == ["1.9.0"]
    assert await sql("SELECT version, status FROM agent_releases ORDER BY id") == [("2.0.0", "stopped"),
                                                                                   ("1.9.0", "uploaded")]


async def test_sync_rejects_mismatched_or_invalid_metadata(dirs):
    repo, store = dirs
    put_release(repo, "1.3.0", sha256="0" * 64)            # zip berubah setelah ditandatangani
    put_release(repo, "1.4.0", signature="pendek")
    (repo / "ronda-agent-1.5.0.json").write_text(json.dumps({"version": "1.6.0"}))   # nama ≠ versi
    (repo / "ronda-agent-1.7.0.json").write_text("{bukan json")
    assert await sync(repo) == []
    assert await sql("SELECT id FROM agent_releases") == []
    assert not store.exists() or list(store.iterdir()) == []           # tidak ada sisa file sementara

    put_release(repo, "1.3.0")                             # diperbaiki: dicek ulang karena file berubah
    assert await sync(repo) == ["1.3.0"]


async def test_missing_repo_dir_is_ignored(tmp_path):
    assert await sync(tmp_path / "tidak-ada") == []


async def start_release(version: str = "1.4.0", ring_age_hours: float = 0) -> None:
    await sql_exec("INSERT INTO agent_releases (version, sha256, signature, file_path, size_bytes, status, current_ring,"
                   " ring_started_at) VALUES (:v, :s, :g, 'x.zip', 1, 'rolling', 0,"
                   " NOW() - INTERVAL :h MINUTE)", v=version, s="a" * 64, g="A" * 88, h=int(ring_age_hours * 60))


async def release_state():
    return (await sql("SELECT status, current_ring FROM agent_releases"))[0]


async def test_auto_promote_waits_for_hold_then_promotes(client):
    host_id, token = await online_host(client)             # agent 1.3.0
    await sql_exec("UPDATE hosts SET ring = 0 WHERE id = :h", h=host_id)
    await start_release(ring_age_hours=25)

    check = await promote()                               # sudah 25 jam, tapi PC Ring 0 belum terpasang
    assert check.action == "wait" and "belum ada PC" in check.reason
    await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload(agent_version="1.4.0"))
    assert (await promote()).action == "promote"
    assert await release_state() == ("rolling", 1)
    audit = await sql("SELECT action, username, detail FROM audit_logs WHERE action = 'Naikkan rilis'")
    assert audit[0][1] == "sistem" and "otomatis" in audit[0][2]

    check = await promote()                               # Ring 1 baru mulai: tunggu 72 jam
    assert check.action == "wait" and check.reason == ""
    await sql_exec("UPDATE agent_releases SET ring_started_at = NOW() - INTERVAL 73 HOUR")
    assert (await promote()).action == "promote"
    assert await release_state() == ("rolling", 2)
    assert (await promote()).action == "none"             # Ring 2 = semua PC


async def test_auto_promote_with_empty_ring_promotes_after_hold(client):
    await online_host(client)                              # Ring 2: di luar cakupan Ring 0
    await start_release(ring_age_hours=1)
    assert (await promote()).action == "wait"
    await sql_exec("UPDATE agent_releases SET ring_started_at = NOW() - INTERVAL 25 HOUR")
    assert (await promote()).action == "promote"


async def test_rollback_pauses_release_and_resume_resets_timer(client):
    await make_user("admin1", role="admin")
    host_id, token = await online_host(client)
    await sql_exec("UPDATE hosts SET ring = 0 WHERE id = :h", h=host_id)
    await start_release(ring_age_hours=30)

    await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload(update_status="rollback"))
    check = await promote()
    assert check.action == "pause" and "1 PC rollback" in check.reason
    assert await release_state() == ("paused", 0)
    assert (await sql("SELECT detail FROM audit_logs WHERE action = 'Jeda rilis'"))[0][0].endswith(
        "(otomatis: 1 PC rollback ke versi sebelumnya)")
    assert await promote() is None                         # dijeda: tidak disentuh lagi

    from app.auth import audit
    async with async_session_factory() as db:
        release_id = (await sql("SELECT id FROM agent_releases"))[0][0]
        await agent_service.change_release(db, release_id, "resume", audit.SYSTEM)
    check = await promote()                               # rollback lama sudah dilihat admin; waktu tunggu ulang
    assert check.action == "wait" and await release_state() == ("rolling", 0)


async def test_failed_download_holds_promotion(client):
    host_id, token = await online_host(client)
    await sql_exec("UPDATE hosts SET ring = 0 WHERE id = :h", h=host_id)
    await start_release(ring_age_hours=30)
    await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload(update_status="failed"))
    check = await promote()
    assert check.action == "wait" and "gagal mengunduh" in check.reason
    assert await release_state() == ("rolling", 0)


async def test_auto_promote_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "RELEASE_AUTO_PROMOTE", False)
    await start_release(ring_age_hours=100)
    assert (await promote()).action == "none"
    assert await release_state() == ("rolling", 0)


async def test_hub_shows_auto_promote_status(client):
    await make_user("admin1", role="admin")
    await login(client, "admin1")
    await start_release(ring_age_hours=1)
    page = await client.get("/agents")
    assert page.status_code == 200
    assert "Naik ke Ring 1 otomatis sekitar" in page.text
    await sql_exec("UPDATE agent_releases SET status = 'paused'")
    assert "Dijeda: periksa PC yang rollback" in (await client.get("/agents")).text
