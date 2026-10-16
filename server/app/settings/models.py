from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Setting(Base):
    """Pengaturan umum yang bisa diubah tanpa deploy (nilai JSON). Nilai awal diisi migrasi."""

    __tablename__ = "settings"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
