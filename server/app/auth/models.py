from datetime import datetime

from sqlalchemy import CHAR, BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UBigInt, UInt

USERNAME_MAX_LENGTH = 50
FULL_NAME_MAX_LENGTH = 100
# require_role(...), CLI, dan form user hanya mengenal nilai ini. Hak akses per role ada di kode (PERMISSIONS).
ROLES = ("admin", "teknisi")
ROLE_LABELS = {"admin": "Admin", "teknisi": "Teknisi"}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin','teknisi')", name="ck_users_role"),)

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    username: Mapped[str] = mapped_column(String(USERNAME_MAX_LENGTH), unique=True)
    full_name: Mapped[str] = mapped_column(String(FULL_NAME_MAX_LENGTH))
    password_hash: Mapped[str] = mapped_column(String(255))        # argon2id
    role: Mapped[str] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class UserSession(Base):
    """Session login. PK = SHA-256 dari session ID di cookie (nilai asli tidak disimpan)."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(UInt, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Salinan dari users: hapus session saat role/status berubah (change_role, set_active)
    role: Mapped[str] = mapped_column(String(10))
    username: Mapped[str] = mapped_column(String(USERNAME_MAX_LENGTH))
    expires_at: Mapped[datetime] = mapped_column(index=True)    # idle timeout, diperpanjang saat aktif
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())   # dasar batas mutlak SESSION_MAX_AGE


class LoginFailure(Base):
    """Rate limit login tanpa Redis: setiap percobaan dicatat di sini dulu dan baru dihapus bila login
    berhasil; kegagalan dihitung per (user, IP) dan per IP dalam jendela waktu.
    Menggantikan kolom users.failed_logins/locked_until di desain skema awal: kunci per user saja memungkinkan
    siapa pun mengunci akun orang lain dari IP mana pun."""

    __tablename__ = "login_failures"
    __table_args__ = (
        Index("ix_login_failures_ip", "ip", "created_at"),   # kedua hitungan memfilter ip
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("users.id", ondelete="CASCADE"))
    ip: Mapped[str] = mapped_column(String(45))   # kunci rate limit: IPv4, atau prefix /64 untuk IPv6
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


AUDIT_CATEGORIES = {
    "login": "Login", "remote": "Remote", "screenshot": "Screenshot", "alert": "Alert",
    "pc": "PC", "agent": "Agent", "user": "Hak akses", "report": "Laporan",
}


class AuditLog(Base):
    """Audit Log: aplikasi hanya INSERT + SELECT (grant DB tanpa UPDATE/DELETE, lihat README "Hak akses database").
    Dipartisi per bulan oleh migrasi (PK menyertakan ts), sehingga tanpa FOREIGN KEY; user_id NULL = sistem."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_ts", "ts"),
        Index("ix_audit_user_ts", "user_id", "ts"),
        Index("ix_audit_cat_ts", "category", "ts"),
        Index("ix_audit_sens_ts", "is_sensitive", "ts"),
        Index("ix_audit_target_ts", "target_type", "target_id", "ts"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True, server_default=func.now())
    user_id: Mapped[int | None] = mapped_column(UInt)
    username: Mapped[str] = mapped_column(String(50))        # disalin: tetap terbaca bila user dihapus
    category: Mapped[str] = mapped_column(String(12))
    action: Mapped[str] = mapped_column(String(40))
    is_sensitive: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    target_type: Mapped[str | None] = mapped_column(String(12))   # host|user|release|rule|key
    target_id: Mapped[int | None] = mapped_column(UInt)
    target_label: Mapped[str | None] = mapped_column(String(80))
    detail: Mapped[str | None] = mapped_column(String(500))
    ip: Mapped[str | None] = mapped_column(String(45))
