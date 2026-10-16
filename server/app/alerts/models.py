from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Computed, ForeignKey, Index, SmallInteger, String, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DECIMAL

from app.core.database import Base, UBigInt, UInt

METRICS = {
    "disk_pct": "Disk", "cpu_pct": "CPU", "ram_pct": "RAM", "latency_ms": "Latency",
    "offline_min": "Offline", "agent_rollback": "Agent rollback", "uptime_days": "Uptime",
}
OPERATORS = (">", ">=", "<", "<=", "=")
LEVELS = {"critical": "Kritis", "warning": "Peringatan", "info": "Info"}


class AlertRule(Base):
    __tablename__ = "alert_rules"
    __table_args__ = (
        CheckConstraint(
            "metric IN ('disk_pct','cpu_pct','ram_pct','latency_ms','offline_min','agent_rollback','uptime_days')",
            name="ck_rules_metric",
        ),
        CheckConstraint("operator IN ('>','>=','<','<=','=')", name="ck_rules_op"),
        CheckConstraint("level IN ('critical','warning','info')", name="ck_rules_level"),
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    metric: Mapped[str] = mapped_column(String(16))
    operator: Mapped[str] = mapped_column(String(2))
    threshold: Mapped[Decimal] = mapped_column(DECIMAL(10, 2))
    duration_sec: Mapped[int] = mapped_column(UInt, server_default=text("300"))
    level: Mapped[str] = mapped_column(String(8))
    folder_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("folders.id", ondelete="SET NULL", name="fk_rules_folder"))   # NULL = semua PC
    notify_resolved: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    updated_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("users.id", name="fk_rules_user"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    __table_args__ = (CheckConstraint("kind IN ('telegram','whatsapp')", name="ck_channel_kind"),)

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(100))                  # chat_id / nomor
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))


class AlertRuleChannel(Base):
    __tablename__ = "alert_rule_channels"
    __table_args__ = (Index("ix_arc_channel", "channel_id"),)

    rule_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("alert_rules.id", ondelete="CASCADE", name="fk_arc_rule"), primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("notification_channels.id", ondelete="CASCADE", name="fk_arc_channel"), primary_key=True)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        # Satu alert terbuka per PC + aturan: open_flag NULL setelah selesai, dan NULL boleh berulang di UNIQUE
        UniqueConstraint("host_id", "rule_id", "open_flag", name="uq_alerts_open"),
        Index("ix_alerts_active", "open_flag", "level", "started_at"),
        Index("ix_alerts_started", "started_at"),
        Index("ix_alerts_rule", "rule_id"),
        CheckConstraint("level IN ('critical','warning','info')", name="ck_alerts_level"),
        CheckConstraint("status IN ('new','acknowledged','resolved')", name="ck_alerts_status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True)
    host_id: Mapped[int] = mapped_column(UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_alerts_host"))
    rule_id: Mapped[int] = mapped_column(UInt, ForeignKey("alert_rules.id", name="fk_alerts_rule"))
    level: Mapped[str] = mapped_column(String(8))                     # disalin dari aturan saat dibuat
    value: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    message: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(12), server_default="new")
    started_at: Mapped[datetime]
    acknowledged_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("users.id", name="fk_alerts_ack"))
    acknowledged_at: Mapped[datetime | None]
    muted_until: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("users.id", name="fk_alerts_res"))
    resolve_note: Mapped[str | None] = mapped_column(String(200))     # tambahan: "Otomatis · listrik padam"
    notified_at: Mapped[datetime | None]
    open_flag: Mapped[int | None] = mapped_column(
        SmallInteger, Computed("IF(resolved_at IS NULL, 1, NULL)", persisted=True))


class AlertNote(Base):
    __tablename__ = "alert_notes"
    __table_args__ = (Index("ix_notes_alert", "alert_id"),)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True)
    alert_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("alerts.id", ondelete="CASCADE", name="fk_notes_alert"))
    user_id: Mapped[int] = mapped_column(UInt, ForeignKey("users.id", name="fk_notes_user"))
    note: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
