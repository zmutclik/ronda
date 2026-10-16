"""Batas field dari server/app/hosts/schemas.py. Nilai dijepit/dipotong di sini agar server tidak menolak 422."""
from datetime import UTC, datetime

HOSTNAME_MAX = 63
VERSION_MAX = 20
SERIAL_MAX = 60
MOUNT_MAX = 16
USER_MAX = 64
LATENCY_MAX = 99999.0
DISKS_MAX = 26
BATCH_MAX = 500          # sampel per batch metrics (samples dan disks masing-masing)
FAIL_REASON_MAX = 200


def pct(value: float) -> float:
    """Persen 1 desimal, dijepit 0–100."""
    return round(min(100.0, max(0.0, float(value))), 1)


def latency(value: float | None) -> float | None:
    return None if value is None else round(min(LATENCY_MAX, max(0.0, float(value))), 1)


def text(value: str | None, limit: int) -> str | None:
    """Potong string; string kosong setelah strip dianggap tidak ada."""
    if value is None:
        return None
    value = str(value).strip()[:limit].strip()
    return value or None


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(ts: datetime) -> str:
    """ISO 8601 dengan zona (server menolak timestamp tanpa zona). Format tetap agar urut secara leksikal."""
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
