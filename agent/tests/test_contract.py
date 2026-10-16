"""Kontrak API: agent sungguhan (psutil di mesin ini) melawan server Ronda asli (uvicorn + MariaDB `_test`).

    TEST_DATABASE_URL="mysql+asyncmy://ronda:ronda@127.0.0.1:33061/ronda_test" uv run pytest -m contract

Database dikosongkan sebelum dan sesudah tiap test, sama seperti test suite server.
"""
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from ronda_agent.api import Api, make_http_client
from ronda_agent.buffer import Buffer
from ronda_agent.config import StateStore
from ronda_agent.main import Agent, SystemCollectors
from ronda_agent.release import UPDATE_EXIT_CODE
from tests.test_inventory import RAW
from tests.test_release import PUB, make_zip, offer_for

pytestmark = pytest.mark.contract

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
SERVER_PY = SERVER_DIR / ".venv" / "bin" / "python"
HELPER = Path(__file__).with_name("server_helper.py")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL belum di-set", allow_module_level=True)
if not TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0].endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL harus menunjuk database *_test (dikosongkan oleh test ini)")


RELEASE_DIR = Path(tempfile.mkdtemp(prefix="ronda-releases-"))


def _server_env() -> dict:
    return {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "SECRET_KEY": "test-secret-" + "x" * 32,
            "COOKIE_SECURE": "false", "RELEASE_DIR": str(RELEASE_DIR)}


def helper(*args: str):
    out = subprocess.run([str(SERVER_PY), str(HELPER), *args], cwd=SERVER_DIR, env=_server_env(),
                         check=True, capture_output=True, text=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def server_url():
    subprocess.run([str(SERVER_PY), "-m", "alembic", "upgrade", "head"], cwd=SERVER_DIR, env=_server_env(),
                   check=True, capture_output=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([str(SERVER_PY), "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level",
                             "warning"], cwd=SERVER_DIR, env=_server_env())
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server tidak menyala")
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture
def agent_env(server_url, tmp_path):
    helper("truncate")
    key = helper("key")
    (tmp_path / "config.toml").write_text(f'server_url = "{server_url}"\nenrollment_key = "{key}"\n')
    clients = []

    def make_agent():
        http = make_http_client(server_url, None)
        clients.append(http)
        return Agent(tmp_path, Api(http), SystemCollectors(server_url), Buffer(tmp_path / "buffer.db"),
                     StateStore(tmp_path / "state.dat"))

    yield tmp_path, make_agent
    for c in clients:
        c.close()
    helper("truncate")


def test_enroll_pending_approve_and_metrics(agent_env):
    data_dir, make_agent = agent_env
    agent = make_agent()
    agent.tick()                                    # enroll + heartbeat pertama
    assert agent.phase == "pending"
    host_id = agent.state.host_id
    stats = helper("stats", str(host_id))
    assert stats["status"] == "pending" and stats["hostname"] == socket.gethostname()[:63]
    assert stats["metrics"] == 0                    # pending: server belum menerima metrik
    assert agent.buffer.counts()[0] == 1

    helper("approve", str(host_id))
    agent.due["heartbeat"] = agent.clock()          # jangan tunggu 30 detik
    agent.tick()
    assert agent.phase == "online"
    assert agent.buffer.counts() == (0, 0)          # buffer terkirim setelah disetujui
    stats = helper("stats", str(host_id))
    assert stats["status"] == "online"
    assert stats["metrics"] == 1
    assert stats["disk_metrics"] >= 1


def test_rejected_pc_gets_401_and_stops(agent_env):
    data_dir, make_agent = agent_env
    agent = make_agent()
    agent.tick()
    helper("reject", str(agent.state.host_id))
    agent.due["heartbeat"] = agent.clock()
    agent.tick()
    assert agent.phase == "revoked"


def test_screenshot_request_is_reported_failed(agent_env):
    data_dir, make_agent = agent_env
    agent = make_agent()
    agent.tick()
    host_id = agent.state.host_id
    helper("approve", str(host_id))
    agent.due["heartbeat"] = agent.clock()
    agent.tick()
    helper("screenshot", str(host_id))
    agent.due["heartbeat"] = agent.clock()
    agent.tick()                                    # screenshot = false (bawaan): dilaporkan gagal
    agent.screenshot_job.thread.join(10)
    assert helper("stats", str(host_id))["screenshot"] == "failed"


def test_screenshot_uploaded_through_real_helper(agent_env):
    import threading

    from ronda_agent.pipe import HelperClient, address_for, ensure_token
    from ronda_helper.main import HelperServer
    from tests.test_helper import jpeg

    data_dir, make_agent = agent_env
    with (data_dir / "config.toml").open("a") as f:
        f.write("screenshot = true\n")
    server = HelperServer(address_for(data_dir, 0), ensure_token(data_dir), lambda w, b: (jpeg(), 64, 48))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        agent = make_agent()
        agent.helper = HelperClient(data_dir, session=lambda: 0, verify_server=None)
        agent.tick()
        host_id = agent.state.host_id
        helper("approve", str(host_id))
        helper("screenshot", str(host_id))
        agent.due["heartbeat"] = agent.clock()
        agent.tick()
        agent.screenshot_job.thread.join(10)
        assert helper("stats", str(host_id))["screenshot"] == "uploaded"
    finally:
        server.close()


def test_resent_samples_are_idempotent(agent_env):
    data_dir, make_agent = agent_env
    agent = make_agent()
    agent.tick()
    helper("approve", str(agent.state.host_id))
    sample = agent.last_sample
    for _ in range(2):                              # kirim ulang dari buffer: server menimpa, tidak dobel
        agent.api.metrics([sample], [])
    assert helper("stats", str(agent.state.host_id))["metrics"] == 1


def test_update_from_real_release(agent_env):
    """Admin unggah paket bertanda tangan + rilis sampai Ring 2 → agent unduh, verifikasi, siap pindah versi."""
    data_dir, make_agent = agent_env
    (data_dir / "config.toml").write_text((data_dir / "config.toml").read_text() + "update_jitter_sec = 0\n")
    agent = make_agent()
    agent.release_keys = (PUB,)
    agent.tick()
    host_id = agent.state.host_id
    helper("approve", str(host_id))
    data = make_zip("1.1.0")
    package = data_dir / "upload.zip"
    package.write_bytes(data)
    assert helper("release", "1.1.0", str(package), offer_for(data)["signature"]) == offer_for(data)["sha256"]

    agent.due["heartbeat"] = agent.clock()
    agent.tick()                                    # online → cek update → unduh di thread
    assert agent.update_job is not None, agent.update_status
    agent.update_job.thread.join(30)
    agent.due["heartbeat"] = agent.clock()
    agent.tick()
    assert agent.exit_code == UPDATE_EXIT_CODE
    assert (data_dir / "pending_switch").read_text() == "1.1.0"
    assert (data_dir / "versions" / "1.1.0" / "ronda-agent" / "ronda-agent.exe").exists()
    assert helper("agent_version", str(host_id)) == "1.0.0 downloading"


def test_inventory_and_apps_from_this_machine(agent_env):
    """Inventaris sungguhan mesin ini (psutil) + daftar aplikasi diterima dan disimpan server."""
    from ronda_agent.collect import apps
    data_dir, make_agent = agent_env
    agent = make_agent()
    agent.collect.apps = lambda: apps.normalize(RAW)        # registry Windows tidak ada di Linux
    agent.tick()
    host_id = agent.state.host_id
    helper("approve", str(host_id))
    agent.due["heartbeat"] = agent.clock()
    agent.tick()
    inv = helper("inventory", str(host_id))
    assert inv["os_name"] and inv["ram_total_mb"] > 0 and inv["disks"] >= 1 and inv["adapters"] >= 1
    assert inv["server_route"] is None      # server di 127.0.0.1: rutenya loopback, dan loopback tidak dikirim
    assert inv["apps"] == len(apps.normalize(RAW))
    assert agent.state.apps_hash

    helper("refresh_inventory", str(host_id))                # aksi admin "Refresh inventaris"
    sent = []
    original = agent.api.apps
    agent.api.apps = lambda digest, items: sent.append(digest) or original(digest, items)
    agent.due["heartbeat"] = agent.clock()
    agent.tick()                                             # heartbeat membawa perintah → jadwal inventaris
    agent.tick()
    assert sent == [agent.state.apps_hash]                   # dikirim ulang walau hash sama
