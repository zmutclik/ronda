"""Remote: DES/ini UltraVNC, jabat tangan RFB dua sisi, tunnel lengkap (VNC palsu + relay WebSocket palsu yang
berperan sebagai browser noVNC), pelaporan gagal, dan perintah `remote` di loop agent."""
import socket
import struct
import threading

import httpx
import pytest
from websockets.sync.client import connect
from websockets.sync.server import serve

from ronda_agent.api import Api
from ronda_agent.pipe import HelperClient, ensure_token
from ronda_agent.remote import (
    DISABLED_REASON, MISSING_REASON, RemoteError, RemoteJob, _vnc_key, ini_password, open_relay, ultravnc_ini,
    vnc_response,
)
from ronda_helper.main import HelperServer
from tests.fakes import FakeServer, run_for

# ---------- DES & ini ----------


def test_vnc_des_matches_known_vectors():
    # Kunci tetap VNC {23,82,107,6,35,78,88,7} versi bit-terbalik = kunci DES standar yang dikenal umum
    assert _vnc_key(bytes([23, 82, 107, 6, 35, 78, 88, 7]).decode("latin-1")).hex() == "e84ad660c4721ae0"
    # Password "password" tersimpan (registry TightVNC/RealVNC, ultravnc.ini) = dbd83cfd727a1458
    stored = ini_password("password")
    assert stored[:16] == "DBD83CFD727A1458"
    assert int(stored[16:], 16) == sum(bytes.fromhex(stored[:16])) & 0xFF     # checksum WritePrivateProfileStruct
    assert len(vnc_response("password", bytes(16))) == 16


def test_ultravnc_ini_loopback_only_and_view_only_disables_input():
    ini = ultravnc_ini("full1234", "view5678", view_only=True, port=5939)
    lines = set(ini.splitlines())
    assert {"LoopbackOnly=1", "AllowLoopback=1", "HTTPConnect=0", "PortNumber=5939", "InputsEnabled=0",
            "FileTransferEnabled=0", "AllowProperties=0", f"passwd={ini_password('full1234')}",
            f"passwd2={ini_password('view5678')}"} <= lines
    assert "InputsEnabled=1" in ultravnc_ini("a", "b", view_only=False).splitlines()


def test_open_relay_only_to_relay_agent_path():
    for path in ("/api/agent/heartbeat", "/relay/agent/1/../../x", "//evil.example/relay/agent/1",
                 "/relay/agent/1?x=1"):
        with pytest.raises(RemoteError):
            open_relay("https://ronda.test", None, "tok", path)


# ---------- VNC & relay palsu ----------

class FakeVnc:
    """UltraVNC palsu: RFB 3.8, VNC auth dengan password yang ditulis start() (view-only memakai passwd2)."""

    def __init__(self, *, fail_start: str | None = None, wrong_password: bool = False):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.fail_start, self.wrong_password = fail_start, wrong_password
        self.started: list[tuple[str, str, bool]] = []
        self.stops = 0
        self.client_init: bytes | None = None
        threading.Thread(target=self._serve, daemon=True).start()

    def start(self, full, view, *, view_only):
        self.started.append((full, view, view_only))
        if self.fail_start:
            raise RemoteError(self.fail_start)

    def stop(self):
        self.stops += 1

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                self._session(conn)

    def _session(self, conn):
        full, view, view_only = self.started[-1]
        conn.sendall(b"RFB 003.008\n")
        assert conn.recv(12) == b"RFB 003.008\n"
        conn.sendall(bytes([1, 2]))
        assert conn.recv(1) == b"\x02"
        challenge = bytes(range(16))
        conn.sendall(challenge)
        expected = vnc_response(view if view_only else full, challenge)
        if conn.recv(16) != expected or self.wrong_password:
            reason = b"Authentication failed"
            conn.sendall(struct.pack(">I", 1) + struct.pack(">I", len(reason)) + reason)
            return
        conn.sendall(struct.pack(">I", 0))
        self.client_init = conn.recv(1)
        conn.sendall(b"SERVERINIT")
        while data := conn.recv(1024):
            conn.sendall(data.upper())             # "layar" = gema huruf besar


class FakeRelay:
    """Relay + browser noVNC sekaligus: jabat tangan sebagai client RFB, ClientInit, lalu kirim/terima data."""

    def __init__(self):
        self.result: dict = {}
        self.paths: list[str] = []
        self.server = serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self, ws):
        self.paths.append(ws.request.path)
        self.result["auth"] = ws.request.headers.get("Authorization")
        try:
            self.result["version"] = ws.recv(5)
            ws.send(b"RFB 003.008\n")
            self.result["types"] = ws.recv(5)
            ws.send(b"\x01" + b"\x01")              # pilih None + ClientInit (shared) dalam satu pesan
            self.result["security_result"] = ws.recv(5)
            self.result["server_init"] = ws.recv(5)
            ws.send(b"halo layar")
            self.result["echo"] = ws.recv(5)
        except Exception as err:
            self.result["error"] = repr(err)

    def connect(self, path):
        return connect(f"ws://127.0.0.1:{self.port}{path}", additional_headers={"Authorization": "Bearer tok-1"})

    def close(self):
        self.server.shutdown()


@pytest.fixture
def api():
    server = FakeServer()
    server.status = "online"
    http = httpx.Client(base_url="https://ronda.test", transport=httpx.MockTransport(server))
    yield Api(http, token=server.token), server
    http.close()


def job(api_, vnc, relay, *, mode="control", enabled=True, notices=None):
    return RemoteJob(api_, 7, mode=mode, user="admin.budi", relay_path="/relay/agent/7", vnc=vnc,
                     connect=relay.connect, notify=(notices.append if notices is not None else None),
                     enabled=enabled)


def run(j: RemoteJob) -> RemoteJob:
    j.start().thread.join(15)
    assert j.result == "done"
    return j


@pytest.mark.parametrize("mode", ["control", "view"])
def test_tunnel_end_to_end(api, mode):
    api_, server = api
    vnc, relay, notices = FakeVnc(), FakeRelay(), []
    run(job(api_, vnc, relay, mode=mode, notices=notices))
    relay.close()
    r = relay.result
    assert "error" not in r, r
    assert relay.paths == ["/relay/agent/7"] and r["auth"] == "Bearer tok-1"
    assert r["version"] == b"RFB 003.008\n" and r["types"] == b"\x01\x01" and r["security_result"] == bytes(4)
    assert r["server_init"] == b"SERVERINIT" and r["echo"] == b"HALO LAYAR"
    assert vnc.client_init == b"\x01"                # ClientInit browser diteruskan ke VNC apa adanya
    [(full, view, view_only)] = vnc.started
    assert view_only is (mode == "view") and full != view and len(full) == 8
    assert vnc.stops == 1 and server.fails == []
    assert "mulai " + ("mengendalikan" if mode == "control" else "melihat") in notices[0]
    assert "admin.budi" in notices[0] and notices[1] == "Sesi remote oleh Tim IT telah selesai."


def test_disabled_missing_and_start_failure_reported(api):
    api_, server = api
    relay = FakeRelay()
    run(job(api_, FakeVnc(), relay, enabled=False))
    run(job(api_, None, relay))
    vnc = FakeVnc(fail_start="UltraVNC tidak mendengarkan port 5939 dalam 20 dtk")
    run(job(api_, vnc, relay))
    assert server.fails == ["7", "7", "7"]
    assert server.fail_reasons == [DISABLED_REASON, MISSING_REASON,
                                   "UltraVNC tidak mendengarkan port 5939 dalam 20 dtk"]
    assert vnc.stops == 1 and relay.paths == []
    relay.close()


def test_relay_unreachable_and_vnc_login_rejected(api):
    api_, server = api
    vnc = FakeVnc()

    class Down:
        def connect(self, path):
            raise OSError("Connection refused")
    run(job(api_, vnc, Down()))
    assert server.fail_reasons == ["Relay tidak terjangkau: Connection refused"] and vnc.stops == 1

    vnc, relay = FakeVnc(wrong_password=True), FakeRelay()
    run(job(api_, vnc, relay))
    relay.close()
    assert server.fail_reasons[-1] == "Login ke UltraVNC gagal: Authentication failed"


def test_helper_shows_remote_notice(tmp_path):
    token = ensure_token(tmp_path)
    shown = []
    from ronda_agent import pipe
    server = HelperServer(pipe.address_for(tmp_path, 0), token, lambda w, b: (b"", 0, 0),
                          lambda text, title="": shown.append((title, text)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        HelperClient(tmp_path, session=lambda: 0, verify_server=None).notify("Tim IT mulai melihat layar")
    finally:
        server.close()
    assert shown == [("Ronda · Remote layar", "Tim IT mulai melihat layar")]


# ---------- loop agent ----------

def remote_agent(env, extra_config: str = ""):
    data_dir, clock, server, make_agent = env
    if extra_config:
        with (data_dir / "config.toml").open("a") as f:
            f.write(extra_config)
    server.status = "online"
    server.commands = [{"type": "remote", "session_id": 7, "mode": "control", "user": "admin.budi",
                        "reason": "tiket", "relay_path": "/relay/agent/7"}]
    return make_agent()


def settle(agent, clock, seconds=120):
    for _ in range(int(seconds / 5)):
        agent.tick()
        if agent.remote_job:
            agent.remote_job.thread.join(5)
        clock.advance(5)


def test_agent_handles_repeated_remote_command_once(env):
    _, clock, server, _ = env
    agent = remote_agent(env)                        # tanpa UltraVNC (di luar Windows)
    settle(agent, clock)                             # server mengulang perintah di tiap heartbeat
    assert server.fail_reasons == [MISSING_REASON]
    assert agent.remote_job is None


def test_agent_remote_disabled_in_config(env):
    _, clock, server, _ = env
    agent = remote_agent(env, "remote = false\n")    # installer /REMOTE=0
    run_for(agent, clock, 60)
    if agent.remote_job:
        agent.remote_job.thread.join(5)
    assert server.fail_reasons == [DISABLED_REASON]


# ---------- lokasi setelan UltraVNC ----------

def test_ultravnc_writes_service_ini_and_legacy_ini(tmp_path):
    from ronda_agent.remote import UltraVnc
    vnc = UltraVnc(tmp_path / "uvnc", config_dir=tmp_path / "ProgramData" / "UltraVNC")
    # UltraVNC 1.8 service mode: winvnc -config <ProgramData>\UltraVNC\<service>.ini -service_run
    assert vnc.ini_paths == [tmp_path / "ProgramData" / "UltraVNC" / "RondaVNC.ini", tmp_path / "uvnc" / "ultravnc.ini"]


def test_write_ini_never_writes_through_planted_tmp(tmp_path):
    from ronda_agent.remote import write_ini
    victim = tmp_path / "system.dll"
    victim.write_text("asli")
    target = tmp_path / "RondaVNC.ini"
    (tmp_path / "RondaVNC.ini.tmp").hardlink_to(victim)      # ditanam user sebelum sesi
    restricted = []
    write_ini(target, "[ultravnc]\n", restricted.append)
    assert victim.read_text() == "asli" and target.read_text() == "[ultravnc]\n"
    assert restricted == [tmp_path / "RondaVNC.ini.tmp"]      # ACL dipasang sebelum file berisi password dipakai


def test_config_dir_owned_by_user_is_refused(tmp_path):
    from ronda_agent.remote import ensure_config_dir
    folder = tmp_path / "UltraVNC"
    locked = []
    ensure_config_dir(folder, lambda p: "S-1-5-21-1-2-3-1001", locked.append)   # belum ada: dibuat & dikunci
    assert folder.is_dir() and locked == [folder]
    ensure_config_dir(folder, lambda p: "S-1-5-32-544")                          # milik Administrators: dipakai
    with pytest.raises(RemoteError, match="dimiliki akun lain"):
        ensure_config_dir(folder, lambda p: "S-1-5-21-1-2-3-1001")               # dibuat user biasa: ditolak
