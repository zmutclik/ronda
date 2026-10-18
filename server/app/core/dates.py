"""Tanggal dari query string (filter laporan & Audit Log)."""
from datetime import date

# Di luar rentang ini konversi zona waktu bisa overflow (mis. 0001-01-01 +07:00 → tahun 0) → 500
MIN_DAY = date(2000, 1, 1)
MAX_DAY = date(2100, 12, 31)


def parse_day(value: str | None, default: date) -> date:
    """ISO "YYYY-MM-DD" (kosong = default). ValueError bila formatnya salah atau di luar MIN_DAY–MAX_DAY."""
    day = date.fromisoformat(value) if value else default
    if not MIN_DAY <= day <= MAX_DAY:
        raise ValueError(f"tanggal di luar {MIN_DAY}–{MAX_DAY}")
    return day
