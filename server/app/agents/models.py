from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UInt, UTinyInt


class EnrollmentKey(Base):
    """Key bersama untuk pendaftaran agent. Hanya hash yang disimpan; key mentah tampil sekali saat dibuat."""

    __tablename__ = "enrollment_keys"

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    key_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)       # SHA-256 dari key
    key_prefix: Mapped[str] = mapped_column(String(24))                # tampilan, mis. "RS-2026-Q4"
    created_by: Mapped[int] = mapped_column(UInt, ForeignKey("users.id", name="fk_enroll_user"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    use_count: Mapped[int] = mapped_column(UInt, server_default=text("0"))


RING_LABELS = {0: "Ring 0 · PC unit IT", 1: "Ring 1 · ±10% PC", 2: "Ring 2 · semua PC"}


class AgentRelease(Base):
    __tablename__ = "agent_releases"
    __table_args__ = (
        CheckConstraint("current_ring IN (0,1,2)", name="ck_release_ring"),
        CheckConstraint("status IN ('uploaded','rolling','paused','stopped','completed')", name="ck_release_status"),
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    version: Mapped[str] = mapped_column(String(20), unique=True)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    signature: Mapped[str] = mapped_column(String(128))                # Ed25519, base64 (dibuat di mesin build)
    file_path: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(UInt)
    notes: Mapped[str | None] = mapped_column(String(500))
    current_ring: Mapped[int] = mapped_column(UTinyInt, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(10), server_default="uploaded")
    ring_started_at: Mapped[datetime | None]
    uploaded_by: Mapped[int] = mapped_column(UInt, ForeignKey("users.id", name="fk_release_user"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
