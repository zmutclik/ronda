"""config.toml (diisi installer) dan state.dat (identitas agent setelah enroll)."""
import json
import logging
import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def default_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Ronda"
    return Path("data")     # development di Linux


class ConfigError(Exception):
    pass


@dataclass
class Config:
    server_url: str
    enrollment_key: str | None = None
    ca_bundle: str | None = None
    heartbeat_sec: int = 30
    sample_sec: int = 30
    latency_sec: int = 60
    disk_sec: int = 300
    metrics_batch_sec: int = 300
    inventory_hours: int = 24
    update_hours: int = 4
    update_jitter_sec: int = 1800     # jeda acak sebelum unduh: ratusan PC tidak mengunduh bersamaan
    screenshot: bool = True           # kebijakan disetujui manajemen; matikan per PC: installer /SCREENSHOT=0


_INTERVALS = {"heartbeat_sec": 10, "sample_sec": 10, "latency_sec": 10, "disk_sec": 30, "metrics_batch_sec": 30,
              "inventory_hours": 1, "update_hours": 1, "update_jitter_sec": 0}


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))     # installer menulis UTF-8 dengan BOM
    except FileNotFoundError as err:
        raise ConfigError(f"{path} tidak ada") from err
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path} tidak valid: {err}") from err
    url = str(raw.get("server_url", "")).strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ConfigError("server_url wajib diisi dengan https://…")
    cfg = Config(server_url=url, enrollment_key=(str(raw.get("enrollment_key") or "").strip() or None),
                 ca_bundle=(str(raw.get("ca_bundle") or "").strip() or None))
    for name, minimum in _INTERVALS.items():
        if name in raw:
            value = raw[name]
            if not isinstance(value, int) or value < minimum:
                raise ConfigError(f"{name} harus bilangan bulat ≥ {minimum}")
            setattr(cfg, name, value)
    if "screenshot" in raw:
        if not isinstance(raw["screenshot"], bool):
            raise ConfigError("screenshot harus true atau false")
        cfg.screenshot = raw["screenshot"]
    return cfg


def remove_enrollment_key(path: Path) -> None:
    """Hapus baris enrollment_key setelah enroll berhasil; baris lain (termasuk komentar) dibiarkan."""
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    kept = [line for line in lines if not line.lstrip().startswith("enrollment_key")]
    if kept != lines:
        _atomic_write(path, "".join(kept).encode("utf-8"))


# ---------- state.dat ----------

@dataclass
class State:
    agent_token: str | None = None
    host_id: int | None = None
    apps_hash: str | None = None
    extra: dict = field(default_factory=dict)


class _Plain:
    """Development di luar Windows: tanpa enkripsi."""
    def protect(self, data: bytes) -> bytes:
        return data

    def unprotect(self, data: bytes) -> bytes:
        return data


class _Dpapi:
    """DPAPI scope mesin: hanya bisa dibuka di PC ini (state.dat disalin ke PC lain tidak berguna)."""
    LOCAL_MACHINE = 0x4

    def protect(self, data: bytes) -> bytes:
        import win32crypt
        return win32crypt.CryptProtectData(data, "ronda-state", None, None, None, self.LOCAL_MACHINE)

    def unprotect(self, data: bytes) -> bytes:
        import win32crypt
        return win32crypt.CryptUnprotectData(data, None, None, None, self.LOCAL_MACHINE)[1]


class StateStore:
    def __init__(self, path: Path, protector=None):
        self.path = path
        self.protector = protector or (_Dpapi() if sys.platform == "win32" else _Plain())

    def load(self) -> State:
        try:
            raw = json.loads(self.protector.unprotect(self.path.read_bytes()))
        except FileNotFoundError:
            return State()
        except Exception:      # rusak / dari PC lain: mulai ulang (enroll lagi bila ada key)
            log.error("state.dat tidak bisa dibaca; dianggap kosong")
            return State()
        known = {k: raw.pop(k) for k in ("agent_token", "host_id", "apps_hash") if k in raw}
        return State(**known, extra=raw)

    def save(self, state: State) -> None:
        data = asdict(state)
        data.update(data.pop("extra"))
        _atomic_write(self.path, self.protector.protect(json.dumps(data).encode()))


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
