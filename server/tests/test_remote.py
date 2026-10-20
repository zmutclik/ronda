"""Remote: sesi (hak akses, perintah heartbeat, akhiri, gagal dari agent, kedaluwarsa) dan relay WebSocket asli
(proses uvicorn terpisah, seperti service `relay` di docker-compose)."""
import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.auth.dependencies import SESSION_COOKIE
from tests.conftest import (
    PROJECT_DIR, bearer, heartbeat_payload, htmx_headers, login, make_user, online_host, sql, sql_exec,
)


async def start(client, host_id: int, csrf: str, mode: str = "view", reason: str = "tiket #2318"):
    return await client.post(f"/remote/hosts/{host_id}", data={"mode": mode, "reason": reason},
                             headers=htmx_headers(csrf))


async def remote_commands(client, token: str) -> list[dict]:
    resp = await client.post("/api/agent/heartbeat", headers=bearer(token), json=heartbeat_payload())
    return [c for c in resp.json()["commands"] if c["type"] == "remote"]


async def test_start_session_permissions_and_heartbeat_command(client):
    host_id, token = await online_host(client)
    await make_user("tek", role="teknisi")
    csrf = await login(client, "tek")
    assert (await start(client, host_id, csrf, mode="control")).status_code == 403   # kontrol penuh: Admin
    resp = await start(client, host_id, csrf, reason="x")
    assert resp.status_code == 422 and "Alasan wajib" in resp.text
    resp = await start(client, host_id, csrf)
    assert resp.status_code == 204
    session_id = int(resp.headers["HX-Redirect"].rsplit("/", 1)[1])

    assert await remote_commands(client, token) == [{
        "type": "remote", "session_id": session_id, "mode": "view", "user": "tek", "reason": "tiket #2318",
        "relay_path": f"/relay/agent/{session_id}"}]
    # Satu sesi per PC
    resp = await start(client, host_id, csrf)
    assert resp.status_code == 422 and "sedang di-remote oleh tek" in resp.text
    assert (await sql("SELECT action, is_sensitive FROM audit_logs WHERE category = 'remote'")) == [("Mulai remote", 1)]
    assert await sql("SELECT id FROM host_events WHERE kind = 'remote' AND message LIKE 'Remote (lihat saja) oleh tek%'")

    # Sudah tersambung di relay: perintah tidak dititipkan lagi
    await sql_exec("UPDATE remote_sessions SET connected_at = NOW()")
    assert await remote_commands(client, token) == []

    # PC offline: tidak bisa
    await sql_exec("UPDATE remote_sessions SET ended_at = NOW()")
    await sql_exec("UPDATE hosts SET last_seen_at = NOW() - INTERVAL 1 HOUR")
    resp = await start(client, host_id, csrf)
    assert resp.status_code == 422 and "offline" in resp.text


async def test_viewer_status_and_stop_only_for_owner(client):
    host_id, _ = await online_host(client)
    await make_user("adm", role="admin")
    await make_user("tek", role="teknisi")
    csrf = await login(client, "adm")
    session_id = int((await start(client, host_id, csrf, mode="control")).headers["HX-Redirect"].rsplit("/", 1)[1])
    page = await client.get(f"/remote/{session_id}")
    assert page.status_code == 200 and 'data-view-only="0"' in page.text and "Ctrl+Alt+Del" in page.text
    assert "Menunggu PC" in (await client.get(f"/remote/{session_id}/status")).text
    tab = (await client.get(f"/hosts/{host_id}?tab=remote")).text
    assert f'href="/remote/{session_id}"' in tab and "menunggu PC" in tab

    csrf_tek = await login(client, "tek")
    assert (await client.get(f"/remote/{session_id}")).status_code == 404
    assert (await client.post(f"/remote/{session_id}/stop", headers=htmx_headers(csrf_tek))).status_code == 404

    csrf = await login(client, "adm")
    resp = await client.post(f"/remote/{session_id}/stop", headers=htmx_headers(csrf))
    assert resp.status_code == 200 and "Selesai · dihentikan" in resp.text and "hx-get" not in resp.text
    assert (await sql("SELECT end_reason FROM remote_sessions"))[0][0] == "stopped"
    actions = [r[0] for r in await sql("SELECT action FROM audit_logs WHERE category = 'remote' ORDER BY id")]
    assert actions == ["Mulai remote", "Akhiri remote"]


async def test_agent_failure_and_expiry(client):
    from app.core.database import async_session_factory
    from app.remote.service import expire_stale

    host_id, token = await online_host(client)
    other_id, other_token = await online_host(client, "IGD-PC09")
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    session_id = int((await start(client, host_id, csrf)).headers["HX-Redirect"].rsplit("/", 1)[1])
    fail = {"error": "Remote dinonaktifkan di PC ini"}
    # PC lain tidak bisa menutup sesi milik PC ini
    assert (await client.post(f"/api/agent/remote/{session_id}/fail", headers=bearer(other_token), json=fail)
            ).status_code == 404
    assert (await client.post(f"/api/agent/remote/{session_id}/fail", headers=bearer(token), json=fail)
            ).status_code == 200
    assert (await sql("SELECT end_reason, error FROM remote_sessions"))[0] == ("failed", fail["error"])
    assert "Remote dinonaktifkan di PC ini" in (await client.get(f"/remote/{session_id}/status")).text

    # Agent tidak pernah tersambung → timeout; tersambung tapi relay mati → expired
    session_id = int((await start(client, other_id, csrf)).headers["HX-Redirect"].rsplit("/", 1)[1])
    await sql_exec("UPDATE remote_sessions SET started_at = NOW() - INTERVAL 10 MINUTE WHERE id = :i", i=session_id)
    async with async_session_factory() as db:
        assert await expire_stale(db) == 1
    assert (await sql("SELECT end_reason FROM remote_sessions WHERE id = :i", i=session_id))[0][0] == "timeout"
    assert await remote_commands(client, other_token) == []


# ---------- relay (proses uvicorn asli) ----------

@pytest.fixture(scope="module")
def relay_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "DEBUG": "false"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.remote.relay:app", "--host", "127.0.0.1", "--port", str(port),
         "--workers", "1", "--no-access-log"],
        cwd=PROJECT_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("relay tidak jalan")
    yield f"ws://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(10)


def browser_ws(base: str, session_id: int, client, origin: str | None = None):
    host = base.removeprefix("ws://")
    return connect(f"{base}/relay/browser/{session_id}", open_timeout=5, additional_headers={
        "Cookie": f"{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}", "Origin": origin or f"http://{host}"})


def agent_ws(base: str, session_id: int, token: str):
    return connect(f"{base}/relay/agent/{session_id}", open_timeout=5, additional_headers=bearer(token))


async def closed_code(ws, timeout: float = 8) -> int:
    with pytest.raises(ConnectionClosed) as info:
        await asyncio.wait_for(ws.recv(), timeout)
    return info.value.rcvd.code if info.value.rcvd else 1006


async def wait_sql(query: str, expected, timeout: float = 8, **params):
    deadline = time.monotonic() + timeout
    while (rows := await sql(query, **params)) != expected and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    assert rows == expected


async def test_relay_pairs_browser_with_agent_and_records_end(client, relay_url):
    host_id, token = await online_host(client)
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    session_id = int((await start(client, host_id, csrf, mode="control")).headers["HX-Redirect"].rsplit("/", 1)[1])

    browser = await browser_ws(relay_url, session_id, client)
    agent = await agent_ws(relay_url, session_id, token)
    await agent.send(b"RFB 003.008\n")              # dikirim agent sebelum dipasangkan: tetap sampai utuh
    assert await asyncio.wait_for(browser.recv(), 5) == b"RFB 003.008\n"
    await browser.send(b"RFB 003.008\n")
    assert await asyncio.wait_for(agent.recv(), 5) == b"RFB 003.008\n"
    await wait_sql("SELECT connected_at IS NOT NULL, ended_at IS NULL FROM remote_sessions", [(1, 1)])

    # Sisi yang sama dua kali (tab kedua) ditolak tanpa mengganggu sesi
    with pytest.raises(InvalidStatus):
        await browser_ws(relay_url, session_id, client)

    await browser.close()
    assert await closed_code(agent) == 4410
    await wait_sql("SELECT end_reason FROM remote_sessions", [("browser_closed",)])


async def test_relay_rejects_foreign_origin_other_user_and_other_pc(client, relay_url):
    host_id, token = await online_host(client)
    _, other_token = await online_host(client, "IGD-PC09")
    await make_user("adm", role="admin")
    await make_user("adm2", role="admin")
    csrf = await login(client, "adm")
    session_id = int((await start(client, host_id, csrf)).headers["HX-Redirect"].rsplit("/", 1)[1])

    with pytest.raises(InvalidStatus):            # halaman situs lain membuka WebSocket dengan cookie user
        await browser_ws(relay_url, session_id, client, origin="https://jahat.example")
    with pytest.raises(InvalidStatus):            # agent PC lain
        await agent_ws(relay_url, session_id, other_token)
    with pytest.raises(InvalidStatus):            # token salah
        await agent_ws(relay_url, session_id, "palsu")
    await login(client, "adm2")
    with pytest.raises(InvalidStatus):            # bukan pembuka sesi
        await browser_ws(relay_url, session_id, client)
    assert (await sql("SELECT connected_at, ended_at FROM remote_sessions"))[0] == (None, None)


async def test_relay_stop_button_and_agent_failure_close_connections(client, relay_url):
    host_id, token = await online_host(client)
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")

    # Tombol Akhiri saat tersambung: kedua sisi diputus relay
    session_id = int((await start(client, host_id, csrf)).headers["HX-Redirect"].rsplit("/", 1)[1])
    browser = await browser_ws(relay_url, session_id, client)
    agent = await agent_ws(relay_url, session_id, token)
    await wait_sql("SELECT connected_at IS NOT NULL FROM remote_sessions", [(1,)])
    await client.post(f"/remote/{session_id}/stop", headers=htmx_headers(csrf))
    assert await closed_code(browser) == 4410
    assert await closed_code(agent) == 4410
    assert (await sql("SELECT end_reason FROM remote_sessions"))[0][0] == "stopped"

    # Agent melapor gagal saat browser masih menunggu: browser diputus, galat tersimpan untuk halaman status
    session_id = int((await start(client, host_id, csrf)).headers["HX-Redirect"].rsplit("/", 1)[1])
    browser = await browser_ws(relay_url, session_id, client)
    await client.post(f"/api/agent/remote/{session_id}/fail", headers=bearer(token),
                      json={"error": "UltraVNC tidak terpasang"})
    assert await closed_code(browser) == 4410
    await wait_sql("SELECT end_reason, error FROM remote_sessions WHERE id = :i",
                   [("failed", "UltraVNC tidak terpasang")], i=session_id)
