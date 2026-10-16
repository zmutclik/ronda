"""Alur agent terhadap server palsu (httpx.MockTransport) dengan jam palsu."""
from ronda_agent.config import StateStore
from ronda_agent.main import RECHECK_SEC
from tests.fakes import run_for


def test_enroll_saves_token_and_removes_key(env):
    data_dir, clock, server, make_agent = env
    agent = make_agent()
    agent.tick()
    assert server.paths()[:2] == ["/api/agent/enroll", "/api/agent/heartbeat"]
    enroll_body = server.calls[0][1]
    assert enroll_body["hostname"] == "IGD-PC03" and enroll_body["ip"] == "10.10.12.31"
    assert "enrollment_key" not in (data_dir / "config.toml").read_text()
    assert StateStore(data_dir / "state.dat").load().agent_token == "tok-1"
    assert (data_dir / "healthy").exists()
    hb = server.calls[1][1]
    assert hb["ts"].endswith("Z") and hb["boot_at"] == "2026-10-16T00:12:00Z"
    assert hb["disks"] == [{"mount": "C:", "used_pct": 71.2}] and hb["update_status"] == "none"

    # restart: tidak enroll lagi
    server.calls.clear()
    make_agent().tick()
    assert server.paths() == ["/api/agent/heartbeat"]


def test_pending_buffers_then_flushes_after_approval(env):
    data_dir, clock, server, make_agent = env
    agent = make_agent()
    run_for(agent, clock, 600)
    assert "/api/agent/metrics" not in server.paths()
    assert agent.buffer.counts()[0] == 20                 # sampel 30 dtk tetap dikumpulkan
    assert server.paths().count("/api/agent/heartbeat") == 20

    server.status = "online"
    run_for(agent, clock, 30)
    assert "/api/agent/metrics" in server.paths()[-3:]
    assert len(server.stored_samples) == 21
    assert agent.buffer.counts() == (0, 0)


def test_metrics_retry_with_backoff_keeps_rows(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    server.fail["/api/agent/metrics"] = 503
    agent.tick()
    rows = agent.buffer.counts()
    assert rows[0] == 1
    first = agent.due["metrics"] - clock.mono()
    assert 4 <= first <= 6
    del server.fail["/api/agent/metrics"]
    run_for(agent, clock, 10, step=1)
    assert agent.buffer.counts() == (0, 0)


def test_invalid_batch_is_dropped(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    server.fail["/api/agent/metrics"] = 422
    agent.tick()
    assert agent.buffer.counts() == (0, 0)
    assert server.paths().count("/api/agent/metrics") == 1


def test_unauthorized_stops_until_new_key(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    agent.tick()
    server.status = "rejected"
    clock.advance(30)
    agent.tick()
    assert agent.phase == "revoked"
    server.calls.clear()
    run_for(agent, clock, RECHECK_SEC - 60, step=60)
    assert server.calls == []

    server.status, server.token = "pending", "tok-2"
    (data_dir / "config.toml").write_text('server_url = "https://ronda.test"\nenrollment_key = "IT-abcdefgh"\n')
    run_for(agent, clock, 120, step=60)
    assert "/api/agent/enroll" in server.paths()
    assert agent.phase == "pending"
    assert StateStore(data_dir / "state.dat").load().agent_token == "tok-2"


def test_bad_enrollment_key_waits_an_hour(env):
    data_dir, clock, server, make_agent = env
    (data_dir / "config.toml").write_text('server_url = "https://ronda.test"\nenrollment_key = "IT-salahsalah"\n')
    agent = make_agent()
    run_for(agent, clock, 600)
    assert server.paths() == ["/api/agent/enroll"]
    assert agent.phase == "enroll"


def test_screenshot_command_reported_failed_once(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    server.commands = [{"type": "screenshot", "request_id": "7f3c"}, {"type": "hapus_semua"}]
    agent = make_agent()
    run_for(agent, clock, 120)                    # server mengulang perintah di tiap heartbeat
    assert server.fails == ["7f3c"]


def test_server_down_heartbeat_keeps_interval(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    server.fail["/api/agent/heartbeat"] = 502
    run_for(agent, clock, 300)
    assert server.paths().count("/api/agent/heartbeat") == 10
    assert agent.phase == "online"            # 5xx bukan alasan berhenti
    assert not (data_dir / "healthy").exists()


def test_buffer_flushed_as_soon_as_server_returns(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    agent.tick()
    server.fail.update({"/api/agent/heartbeat": 502, "/api/agent/metrics": 502})
    run_for(agent, clock, 400)
    assert agent.buffer.counts()[0] > 5
    assert agent.due["metrics"] - clock.mono() > 30      # prasyarat: jadwal backoff masih jauh
    server.fail.clear()
    run_for(agent, clock, 30)                 # heartbeat berikutnya sukses → buffer langsung terkirim
    assert agent.buffer.counts() == (0, 0)
