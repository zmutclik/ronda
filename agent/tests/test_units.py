import json
import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ronda_agent import schemas
from ronda_agent.api import Api, Forbidden, Invalid, NotFound, Transient, Unauthorized
from ronda_agent.backoff import Backoff
from ronda_agent.buffer import MAX_ROWS, Buffer
from ronda_agent.config import ConfigError, State, StateStore, load_config, remove_enrollment_key
from ronda_agent.schemas import iso


# ---------- schemas ----------

def test_pct_rounds_and_clamps():
    assert schemas.pct(12.345) == 12.3
    assert schemas.pct(-3) == 0.0
    assert schemas.pct(100.04) == 100.0
    assert schemas.latency(123456) == schemas.LATENCY_MAX
    assert schemas.latency(None) is None


def test_text_truncates_and_empties():
    assert schemas.text("x" * 70, schemas.HOSTNAME_MAX) == "x" * 63
    assert schemas.text("   ", 10) is None
    assert schemas.text(None, 10) is None


def test_iso_is_utc_with_zone():
    wib = datetime(2026, 10, 16, 10, 40, 5, tzinfo=UTC).astimezone()
    assert iso(wib) == "2026-10-16T10:40:05Z"


# ---------- config & state ----------

def test_load_config_defaults_and_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('server_url = "https://ronda.example.local/"\nenrollment_key = "IT-abcdefgh"\nheartbeat_sec = 45\n')
    cfg = load_config(path)
    assert cfg.server_url == "https://ronda.example.local"
    assert cfg.enrollment_key == "IT-abcdefgh"
    assert cfg.heartbeat_sec == 45 and cfg.metrics_batch_sec == 300


@pytest.mark.parametrize("body", ['server_url = "ftp://x"', 'server_url = "https://x"\nheartbeat_sec = 1',
                                  'server_url = "https://x"\nheartbeat_sec = "30"', "server_url = ["])
def test_load_config_rejects_invalid(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    with pytest.raises(ConfigError):
        load_config(path)


def test_remove_enrollment_key_keeps_other_lines(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# dipasang installer\nserver_url = "https://x"\nenrollment_key = "IT-abcdefgh"\nheartbeat_sec = 30\n')
    remove_enrollment_key(path)
    assert path.read_text() == '# dipasang installer\nserver_url = "https://x"\nheartbeat_sec = 30\n'
    assert load_config(path).enrollment_key is None


def test_state_roundtrip_and_corrupt(tmp_path):
    store = StateStore(tmp_path / "state.dat")
    assert store.load() == State()
    store.save(State(agent_token="tok", host_id=42, apps_hash="ab", extra={"future": 1}))
    assert store.load() == State(agent_token="tok", host_id=42, apps_hash="ab", extra={"future": 1})
    (tmp_path / "state.dat").write_bytes(b"\x00rusak")
    assert store.load() == State()


# ---------- buffer ----------

def _sample(ts: datetime) -> dict:
    return {"ts": iso(ts), "cpu_pct": 1.0, "ram_pct": 2.0, "latency_ms": None, "net_in_bps": 0, "net_out_bps": 0}


def test_buffer_peek_remove_and_idempotent(tmp_path):
    buf = Buffer(tmp_path / "b.db")
    t0 = datetime(2026, 10, 16, tzinfo=UTC)
    for i in range(5):
        buf.add_sample(_sample(t0 + timedelta(seconds=30 * i)))
    buf.add_sample(_sample(t0))                        # ts sama menimpa, tidak dobel
    buf.add_disks([{"ts": iso(t0), "mount": "C:", "used_pct": 50.0}, {"ts": iso(t0), "mount": "D:", "used_pct": 9.0}])
    assert buf.counts() == (5, 2)
    samples, disks = buf.peek(3)
    assert [s["ts"] for s in samples] == [iso(t0 + timedelta(seconds=30 * i)) for i in range(3)]
    buf.remove(samples, disks[:1])
    assert buf.counts() == (2, 1)


def test_buffer_prune_by_age_and_size(tmp_path):
    buf = Buffer(tmp_path / "b.db")
    now = datetime(2026, 10, 16, tzinfo=UTC)
    buf.add_sample(_sample(now - timedelta(days=3)))
    for i in range(MAX_ROWS + 10):
        buf.add_sample(_sample(now - timedelta(seconds=i)))
    assert buf.prune(now) == 11
    samples, _ = buf.peek(1)
    assert samples[0]["ts"] == iso(now - timedelta(seconds=MAX_ROWS - 1))   # yang tertua terbuang


# ---------- backoff ----------

def test_backoff_doubles_with_cap_and_jitter():
    b = Backoff(rng=random.Random(1))
    delays = [b.next_delay() for _ in range(10)]
    for delay, base in zip(delays, [5, 10, 20, 40, 80, 160, 300, 300, 300, 300], strict=True):
        assert base * 0.8 <= delay <= base * 1.2
    b.reset()
    assert b.next_delay() <= 6


# ---------- api ----------

@pytest.mark.parametrize(("status", "exc"), [(401, Unauthorized), (403, Forbidden), (404, NotFound), (422, Invalid),
                                             (500, Transient), (503, Transient)])
def test_api_maps_status(status, exc):
    api = Api(httpx.Client(base_url="http://s", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"detail": "x"}))), token="t")
    with pytest.raises(exc):
        api.heartbeat({})


def test_api_transport_error_is_transient_and_sends_bearer():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        raise httpx.ConnectTimeout("timeout", request=request)

    api = Api(httpx.Client(base_url="http://s", transport=httpx.MockTransport(handler)), token="rahasia")
    with pytest.raises(Transient):
        api.metrics([], [])
    assert seen["auth"] == "Bearer rahasia"


def test_api_enroll_has_no_auth_header():
    def handler(request: httpx.Request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"host_id": 1, "agent_token": "t", "status": "pending"})

    api = Api(httpx.Client(base_url="http://s", transport=httpx.MockTransport(handler)))
    assert api.enroll({"enrollment_key": "IT-abcdefgh"})["host_id"] == 1
    json.dumps(api.enroll({}))


def test_config_from_installer_with_bom_and_screenshot_flag(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes("\ufeff# Ditulis installer\r\nserver_url = \"https://ronda.test\"\r\n"
                     "enrollment_key = \"IT-abcdefgh\"\r\nca_bundle = 'C:\\ProgramData\\Ronda\\ca-rs.pem'\r\n"
                     "screenshot = false\r\n".encode("utf-8"))
    cfg = load_config(path)
    assert cfg.screenshot is False and cfg.ca_bundle == r"C:\ProgramData\Ronda\ca-rs.pem"   # /SCREENSHOT=0
    path.write_text('server_url = "https://ronda.test"\n')
    assert load_config(path).screenshot is True                   # aktif bawaan (kebijakan disetujui)
    path.write_bytes("server_url = \"https://ronda.test\"\r\nenrollment_key = \"IT-abcdefgh\"\r\n".encode())
    remove_enrollment_key(path)
    assert load_config(path).enrollment_key is None
    path.write_text('server_url = "https://ronda.test"\nscreenshot = "ya"\n')
    with pytest.raises(ConfigError):
        load_config(path)
