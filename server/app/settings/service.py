from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings.models import Setting

# Nilai bawaan bila baris belum ada (sama dengan isi awal di migrasi)
DEFAULTS: dict[str, Any] = {
    "offline_after_sec": 120,
    "quiet_hours": {"start": "22:00", "end": "06:00", "critical_still_sent": True},
    "retention": {"metrics_days": 30, "hourly_days": 365, "screenshot_days": 7, "audit_days": 365},
    "auto_approve_subnet": None,
}


async def get_value(db: AsyncSession, name: str) -> Any:
    """Baca satu pengaturan memakai transaksi pemanggil (tidak membuka begin() sendiri)."""
    value = await db.scalar(select(Setting.value).where(Setting.name == name))
    return DEFAULTS.get(name) if value is None else value
