"""Tahap 4: pipe agent ↔ helper (socket Unix sungguhan), kompresi JPEG, alur perintah screenshot, pengawas helper."""
import io
import random
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from PIL import Image

from ronda_agent import pipe
from ronda_agent.pipe import TOKEN_ENV, HelperClient, HelperError, HelperUnavailable, Shot, ensure_token
from ronda_agent.release import Layout
from ronda_helper.capture import CaptureError, encode
from ronda_helper.main import HelperServer
from ronda_launcher.helper import HelperSupervisor
from tests.fakes import run_for


def jpeg(width=64, height=48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "teal").save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def unique_pipe_name(monkeypatch):
    """Nama pipe Windows global (tidak ikut tmp_path), dan listener yang sedang accept() tidak lepas saat close():
    tiap test memakai nama sendiri agar tidak bertabrakan dengan helper dari test sebelumnya."""
    if sys.platform == "win32":
        prefix = rf"\\.\pipe\ronda-test-{uuid.uuid4().hex[:12]}"
        monkeypatch.setattr(pipe, "address_for", lambda data_dir, session: f"{prefix}-{session}")


@pytest.fixture
def helper(tmp_path):
    """Helper sungguhan di thread; `capture` bisa diganti test."""
    token = ensure_token(tmp_path)
    state = {"capture": lambda max_width, max_bytes: (jpeg(), 64, 48), "notices": []}
    server = HelperServer(pipe.address_for(tmp_path, 0), token, lambda w, b: state["capture"](w, b),
                          state["notices"].append)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, state
    server.close()


def client(data_dir, **kw):
    return HelperClient(data_dir, session=lambda: 0, verify_server=None, **kw)


# ---------- pipe ----------

def test_screenshot_roundtrip(helper):
    data_dir, _ = helper
    shot = client(data_dir).screenshot("7f3c")
    assert shot.content_type == "image/jpeg" and (shot.width, shot.height) == (64, 48)
    assert shot.content.startswith(b"\xff\xd8\xff")
    assert client(data_dir).ping()["ok"] is True


def test_user_notified_after_capture_unless_disabled(helper):
    data_dir, state = helper
    client(data_dir).screenshot("7f3c")
    assert len(state["notices"]) == 1 and "Tim IT mengambil gambar layar" in state["notices"][0]
    client(data_dir).screenshot("7f3d", notify=False)
    assert len(state["notices"]) == 1                     # Admin tidak mencentang "Beri notifikasi ke user"

    def broken(max_width, max_bytes):
        raise CaptureError("Layar tidak bisa diambil (terkunci atau sesi tidak aktif)")
    state["capture"] = broken
    with pytest.raises(HelperError):
        client(data_dir).screenshot("7f3e")
    assert len(state["notices"]) == 1                     # gagal mengambil layar: tidak ada pemberitahuan


def test_capture_error_is_forwarded(helper):
    data_dir, state = helper

    def broken(max_width, max_bytes):
        raise CaptureError("Layar tidak bisa diambil (terkunci atau sesi tidak aktif)")
    state["capture"] = broken
    with pytest.raises(HelperError, match="terkunci"):
        client(data_dir).screenshot("7f3c")


def test_wrong_token_rejected(helper):
    data_dir, _ = helper
    (data_dir / "local.token").write_text("b" * 64)
    with pytest.raises(HelperError, match="Autentikasi"):
        client(data_dir).screenshot("7f3c")
    (data_dir / "local.token").unlink()
    with pytest.raises(HelperError, match="local.token"):
        client(data_dir).screenshot("7f3c")


def test_non_image_rejected(helper):
    data_dir, state = helper
    state["capture"] = lambda w, b: (b"C:\\Windows\\System32\\config\\SAM", 10, 10)
    with pytest.raises(HelperError, match="bukan gambar"):
        client(data_dir).screenshot("7f3c")


def test_slow_helper_times_out(helper):
    data_dir, state = helper
    state["capture"] = lambda w, b: (time.sleep(3), (jpeg(), 64, 48))[1]
    started = time.monotonic()
    with pytest.raises(HelperError, match="tidak menjawab"):
        client(data_dir, timeout=0.5).screenshot("7f3c")
    assert time.monotonic() - started < 3


def test_no_helper_or_no_session(tmp_path):
    ensure_token(tmp_path)
    with pytest.raises(HelperUnavailable, match="tidak berjalan"):
        client(tmp_path).screenshot("7f3c")
    with pytest.raises(HelperUnavailable, match="Tidak ada user login"):
        HelperClient(tmp_path, session=lambda: None, verify_server=None).screenshot("7f3c")


def test_owner_check_failure_is_helper_error(helper):
    data_dir, _ = helper

    def refuse(conn):
        raise PermissionError("akses ditolak")
    with pytest.raises(HelperError, match="tidak bisa dicek"):
        HelperClient(data_dir, session=lambda: 0, verify_server=refuse).screenshot("7f3c")


def test_token_created_once(tmp_path):
    first = ensure_token(tmp_path)
    assert len(first) == 64 and ensure_token(tmp_path) == first


# ---------- kompresi ----------

def noise(width, height):
    rng = random.Random(1)
    return Image.frombytes("RGB", (width, height), bytes(rng.getrandbits(8) for _ in range(width * height * 3)))


def test_encode_resizes_to_max_width():
    content, w, h = encode(Image.new("RGB", (1920, 1080), "white"), 1600, 3 * 1024 * 1024)
    assert (w, h) == (1600, 900) and content.startswith(b"\xff\xd8\xff")
    _, w, h = encode(Image.new("RGB", (1024, 768), "white"), 1600, 3 * 1024 * 1024)
    assert (w, h) == (1024, 768)               # tidak diperbesar


def test_encode_falls_back_then_gives_up():
    img = noise(1700, 300)
    first, _, _ = encode(img, 1600, 10**9)
    _, w, _ = encode(img, 1600, len(first) - 1)
    assert w == 1280                            # kualitas 50, lebar 1280
    with pytest.raises(CaptureError):
        encode(img, 1600, 1000)


# ---------- alur agent ----------

class FakeHelper:
    def __init__(self, error: Exception | None = None):
        self.error, self.calls, self.notify = error, [], []

    def screenshot(self, request_id, max_width=1600, *, notify=True):
        self.calls.append(request_id)
        self.notify.append(notify)
        if self.error:
            raise self.error
        return Shot(jpeg(), "image/jpeg", 64, 48)


def screenshot_agent(env, helper, enabled=True):
    """Screenshot aktif bawaan; enabled=False menulis `screenshot = false` (installer /SCREENSHOT=0)."""
    data_dir, clock, server, make_agent = env
    if not enabled:
        with (data_dir / "config.toml").open("a") as f:
            f.write("screenshot = false\n")
    server.status = "online"
    server.commands = [{"type": "screenshot", "request_id": "7f3c"}]
    agent = make_agent()
    agent.helper = helper
    return agent


def settle(agent, clock, seconds=120):
    """Jalankan loop; tunggu thread job screenshot selesai di tiap langkah."""
    for _ in range(int(seconds / 5)):
        agent.tick()
        if agent.screenshot_job:
            agent.screenshot_job.thread.join(5)
        clock.advance(5)


def test_screenshot_uploaded_once(env):
    _, clock, server, _ = env
    fake = FakeHelper()
    agent = screenshot_agent(env, fake)
    settle(agent, clock)                       # server mengulang perintah di tiap heartbeat
    assert fake.calls == ["7f3c"] and server.fails == []
    assert fake.notify == [True]                  # perintah tanpa field notify (server lama): tetap beri tahu
    [(rid, headers, content)] = server.uploads
    assert rid == "7f3c" and content.startswith(b"\xff\xd8\xff")
    assert headers["content-type"] == "image/jpeg" and (headers["x-width"], headers["x-height"]) == ("64", "48")


def test_notify_flag_from_server_passed_to_helper(env):
    _, clock, server, _ = env
    fake = FakeHelper()
    agent = screenshot_agent(env, fake)
    server.commands = [{"type": "screenshot", "request_id": "7f3c", "notify": False}]
    settle(agent, clock)
    assert fake.calls == ["7f3c"] and fake.notify == [False]


def test_no_user_reported_failed(env):
    _, clock, server, _ = env
    agent = screenshot_agent(env, FakeHelper(HelperUnavailable("Helper screenshot tidak berjalan di sesi user")))
    agent.collect.logged_user = lambda: None
    settle(agent, clock)
    assert server.fails == ["7f3c"] and server.fail_reasons == ["Tidak ada user login"]


def test_helper_down_with_user_logged_in(env):
    _, clock, server, _ = env
    agent = screenshot_agent(env, FakeHelper(HelperUnavailable("Helper screenshot tidak berjalan di sesi user")))
    settle(agent, clock)
    assert server.fail_reasons == ["Helper screenshot tidak berjalan di sesi user"]


def test_disabled_in_config_reports_failed(env):
    _, clock, server, _ = env
    fake = FakeHelper()
    agent = screenshot_agent(env, fake, enabled=False)
    settle(agent, clock)
    assert fake.calls == [] and server.fail_reasons == ["Screenshot dinonaktifkan di PC ini"]


def test_upload_retried_when_server_down(env):
    _, clock, server, _ = env
    fake = FakeHelper()
    agent = screenshot_agent(env, fake)
    server.fail["/api/agent/screenshots/7f3c"] = 503
    settle(agent, clock, 30)
    assert server.uploads == [] and len(fake.calls) == 1
    del server.fail["/api/agent/screenshots/7f3c"]
    settle(agent, clock, 60)
    assert len(server.uploads) == 1 and len(fake.calls) == 2


def test_expired_request_not_repeated(env):
    _, clock, server, _ = env
    fake = FakeHelper()
    agent = screenshot_agent(env, fake)
    server.fail["/api/agent/screenshots/7f3c"] = 422
    settle(agent, clock)
    assert fake.calls == ["7f3c"] and server.fails == []
    assert fake.notify == [True]                  # perintah tanpa field notify (server lama): tetap beri tahu


def test_heartbeat_continues_while_helper_busy(env):
    _, clock, server, _ = env
    gate = threading.Event()

    class SlowHelper(FakeHelper):
        def screenshot(self, request_id, max_width=1600, *, notify=True):
            gate.wait(10)
            return super().screenshot(request_id, notify=notify)
    agent = screenshot_agent(env, SlowHelper())
    run_for(agent, clock, 120)
    assert server.paths().count("/api/agent/heartbeat") == 4
    gate.set()
    settle(agent, clock, 30)
    assert len(server.uploads) == 1


# ---------- pengawas helper (launcher) ----------

class FakeProc:
    def __init__(self, session, cmd, env):
        self.session, self.cmd, self.env = session, cmd, env
        self.code: int | None = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, 1


@pytest.fixture
def sup(tmp_path):
    for v in ("1.0.0", "1.1.0"):
        (tmp_path / "versions" / v).mkdir(parents=True)
    t = {"now": 0.0, "session": 1, "enabled": True}
    procs: list[FakeProc] = []

    def spawn(session, cmd, cwd, env):
        procs.append(FakeProc(session, cmd, env))
        return procs[-1]

    s = HelperSupervisor(Layout(tmp_path), lambda: t["session"], spawn, enabled=lambda: t["enabled"],
                         command_for=lambda vdir, data, session: [str(vdir / "ronda-helper.exe")],
                         check_sec=1, restart_delays=(5, 60), now=lambda: t["now"])

    def step(version="1.0.0", seconds=1):
        s.tick(version)
        t["now"] += seconds
    return s, t, procs, step


def test_supervisor_starts_in_console_session_with_token(sup, tmp_path):
    s, t, procs, step = sup
    step()
    [p] = procs
    assert p.session == 1 and Path(p.cmd[0]).parts[-2:] == ("1.0.0", "ronda-helper.exe")
    assert p.env[TOKEN_ENV] == (tmp_path / "local.token").read_text()
    step()
    assert len(procs) == 1


def test_supervisor_follows_version_and_session(sup):
    s, t, procs, step = sup
    step()
    step("1.1.0")                                     # update / rollback: helper versi baru
    assert procs[0].terminated and Path(procs[1].cmd[0]).parts[-2:] == ("1.1.0", "ronda-helper.exe")
    t["session"] = None                               # logoff / layar logon
    step("1.1.0")
    assert procs[1].terminated and len(procs) == 2
    t["session"] = 3                                  # user lain login
    step("1.1.0")
    assert procs[2].session == 3


def test_supervisor_backoff_after_crash(sup):
    s, t, procs, step = sup
    step()
    procs[0].code = 1
    step(seconds=4)                                   # crash terdeteksi di t=1 → boleh jalan lagi t=6
    step()
    assert len(procs) == 1
    step()
    assert len(procs) == 2
    procs[1].code = 1
    step(seconds=30)
    step()
    assert len(procs) == 2                            # jeda kedua 60 dtk
    t["now"] += 60
    step()
    assert len(procs) == 3


def test_supervisor_disabled_and_stop(sup):
    s, t, procs, step = sup
    t["enabled"] = False
    step()
    assert procs == []
    t["enabled"] = True
    step()
    s.stop()
    assert procs[0].terminated


def test_supervisor_reads_config_flag(tmp_path):
    (tmp_path / "config.toml").write_text('server_url = "https://ronda.test"\n')
    s = HelperSupervisor(Layout(tmp_path), lambda: 1, lambda *a: None)
    assert s.enabled() is True                        # aktif bawaan
    (tmp_path / "config.toml").write_text('server_url = "https://ronda.test"\nscreenshot = false\n')
    assert s.enabled() is True                        # /SCREENSHOT=0: helper tetap untuk pemberitahuan remote
    (tmp_path / "config.toml").write_text('server_url = "https://ronda.test"\nscreenshot = false\nremote = false\n')
    assert s.enabled() is False                       # installer /SCREENSHOT=0 /REMOTE=0
    (tmp_path / "config.toml").write_text('rusak = [')
    assert s.enabled() is False
