from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UBigInt, UInt


class ReportDownload(Base):
    """Catatan unduhan laporan (siapa, jenis, parameter). Isi laporan dihitung dari tabel lain saat diminta."""

    __tablename__ = "report_downloads"
    __table_args__ = (
        Index("ix_reports_user", "user_id", "created_at"),
        CheckConstraint("report IN ('inventory','availability','alerts','renewal','remote')", name="ck_reports_kind"),
        CheckConstraint("format IN ('pdf','xlsx')", name="ck_reports_format"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True)
    user_id: Mapped[int] = mapped_column(UInt, ForeignKey("users.id", name="fk_reports_user"))
    report: Mapped[str] = mapped_column(String(12))
    format: Mapped[str] = mapped_column(String(4))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
