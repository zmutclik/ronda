from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR, JSON, Boolean, CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DECIMAL

from app.core.database import Base, UBigInt, UInt, USmallInt, UTinyInt

Pct = DECIMAL(5, 2)
Ms = DECIMAL(7, 1)


class Host(Base):
    __tablename__ = "hosts"
    __table_args__ = (
        Index("ix_hosts_folder_status", "folder_id", "status"),   # Tabel Grup per folder
        Index("ix_hosts_status_seen", "status", "last_seen_at"),  # job cek offline
        Index("ix_hosts_hostname", "hostname"),
        Index("ix_hosts_version", "agent_version"),
        Index("ix_hosts_problem", "problem_level"),                 # Dashboard: PC bermasalah
        CheckConstraint("status IN ('pending','online','offline','rejected','blocked')", name="ck_hosts_status"),
        CheckConstraint("ring IN (0,1,2)", name="ck_hosts_ring"),
        CheckConstraint(
            "update_status IN ('none','waiting','downloading','installed','rollback','failed')",
            name="ck_hosts_update",
        ),
    )

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(63))
    display_name: Mapped[str | None] = mapped_column(String(63))
    folder_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("folders.id", ondelete="SET NULL", name="fk_hosts_folder"))
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    agent_token_hash: Mapped[str | None] = mapped_column(CHAR(64), unique=True)
    enrollment_key_id: Mapped[int | None] = mapped_column(
        UInt, ForeignKey("enrollment_keys.id", name="fk_hosts_key"))
    approved_by: Mapped[int | None] = mapped_column(UInt, ForeignKey("users.id", name="fk_hosts_appr"))
    approved_at: Mapped[datetime | None]
    # versi & update agent
    agent_version: Mapped[str | None] = mapped_column(String(20))
    ring: Mapped[int] = mapped_column(UTinyInt, server_default=text("2"))
    update_status: Mapped[str] = mapped_column(String(12), server_default="none")
    # spesifikasi
    os_name: Mapped[str | None] = mapped_column(String(60))
    os_build: Mapped[str | None] = mapped_column(String(30))
    cpu_model: Mapped[str | None] = mapped_column(String(100))
    cpu_cores: Mapped[int | None] = mapped_column(UTinyInt)
    cpu_threads: Mapped[int | None] = mapped_column(USmallInt)
    ram_total_mb: Mapped[int | None] = mapped_column(UInt)
    manufacturer: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(80))
    serial_number: Mapped[str | None] = mapped_column(String(60))
    inventory_no: Mapped[str | None] = mapped_column(String(40))
    # Tambahan: field spesifikasi yang diubah manual di tab Aksi; store_inventory tidak menimpanya.
    manual_specs: Mapped[list[str] | None] = mapped_column(JSON)
    apps_hash: Mapped[str | None] = mapped_column(CHAR(64))
    last_ip: Mapped[str | None] = mapped_column(String(45))
    last_seen_at: Mapped[datetime | None]
    # Tambahan terhadap desain skema awal:
    # - perintah "Refresh inventaris" dikirim lewat respons heartbeat;
    # - ringkasan alert terbuka paling parah, ditulis evaluator alert (alerts → hosts), agar daftar Komputer
    #   dan Dashboard tidak perlu join ke alerts dan domain hosts tidak bergantung pada domain alerts.
    inventory_requested_at: Mapped[datetime | None]
    problem_level: Mapped[str | None] = mapped_column(String(8))      # critical | warning | info
    problem_text: Mapped[str | None] = mapped_column(String(80))      # "Disk C: 94%"
    problem_count: Mapped[int] = mapped_column(UTinyInt, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @property
    def name(self) -> str:
        return self.display_name or self.hostname


class HostLatest(Base):
    """Kondisi terakhir per PC (1:1), diperbarui tiap heartbeat. Dipisah dari hosts agar daftar tetap sempit."""

    __tablename__ = "host_latest"

    host_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_latest_host"), primary_key=True)
    ts: Mapped[datetime]
    cpu_pct: Mapped[Decimal] = mapped_column(Pct)
    ram_pct: Mapped[Decimal] = mapped_column(Pct)
    disk_max_pct: Mapped[Decimal] = mapped_column(Pct)
    disk_max_mount: Mapped[str | None] = mapped_column(String(16))    # tambahan: "Disk C: 94%"
    latency_ms: Mapped[Decimal | None] = mapped_column(Ms)
    logged_user: Mapped[str | None] = mapped_column(String(64))
    logged_since: Mapped[datetime | None]                            # tambahan: "login sejak 08:31"
    boot_at: Mapped[datetime | None]


class HostDisk(Base):
    __tablename__ = "host_disks"

    host_id: Mapped[int] = mapped_column(
        UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_disks_host"), primary_key=True)
    mount: Mapped[str] = mapped_column(String(16), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(40))
    media: Mapped[str | None] = mapped_column(String(10))
    total_bytes: Mapped[int] = mapped_column(UBigInt)
    used_bytes: Mapped[int] = mapped_column(UBigInt)
    updated_at: Mapped[datetime]


class NetworkAdapter(Base):
    __tablename__ = "network_adapters"
    __table_args__ = (UniqueConstraint("host_id", "name", name="uq_nic_host_name"),)

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    host_id: Mapped[int] = mapped_column(UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_nic_host"))
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(String(120))
    mac: Mapped[str | None] = mapped_column(CHAR(17))
    is_virtual: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    is_up: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    is_server_route: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    speed_mbps: Mapped[int | None] = mapped_column(UInt)
    ssid: Mapped[str | None] = mapped_column(String(64))
    signal_pct: Mapped[int | None] = mapped_column(UTinyInt)
    ipv4: Mapped[str | None] = mapped_column(String(45))
    prefix_len: Mapped[int | None] = mapped_column(UTinyInt)
    gateway: Mapped[str | None] = mapped_column(String(45))
    dns: Mapped[str | None] = mapped_column(String(200))
    dhcp: Mapped[bool | None] = mapped_column(Boolean)
    rx_bps: Mapped[int | None] = mapped_column(UBigInt)
    tx_bps: Mapped[int | None] = mapped_column(UBigInt)
    last_up_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]


class InstalledApp(Base):
    """Tab Aplikasi; diganti seluruhnya saat apps_hash berubah (first_seen_at dipertahankan)."""

    __tablename__ = "installed_apps"
    __table_args__ = (UniqueConstraint("host_id", "name", "version", name="uq_apps_host_name_ver"),)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True)
    host_id: Mapped[int] = mapped_column(UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_apps_host"))
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(60), server_default="")
    publisher: Mapped[str | None] = mapped_column(String(150))
    install_date: Mapped[date | None]
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HostEvent(Base):
    """Tab Riwayat (linimasa kejadian per PC)."""

    __tablename__ = "host_events"
    __table_args__ = (Index("ix_events_host_ts", "host_id", "ts"),)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True)
    host_id: Mapped[int] = mapped_column(UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_events_host"))
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    kind: Mapped[str] = mapped_column(String(10))
    level: Mapped[str | None] = mapped_column(String(8))              # tambahan: warna titik (crit/warn/ok)
    message: Mapped[str] = mapped_column(String(255))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)


# --- Time-series: dipartisi per hari/bulan oleh migrasi; tanpa FOREIGN KEY (tidak didukung pada tabel
# berpartisi). Integritas host_id dijaga aplikasi; partisi dibuat/dihapus oleh hosts/tasks.py.

class Metric(Base):
    __tablename__ = "metrics"

    host_id: Mapped[int] = mapped_column(UInt, primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    cpu_pct: Mapped[Decimal] = mapped_column(Pct)
    ram_pct: Mapped[Decimal] = mapped_column(Pct)
    latency_ms: Mapped[Decimal | None] = mapped_column(Ms)
    net_in_bps: Mapped[int | None] = mapped_column(UBigInt)
    net_out_bps: Mapped[int | None] = mapped_column(UBigInt)


class DiskMetric(Base):
    __tablename__ = "disk_metrics"

    host_id: Mapped[int] = mapped_column(UInt, primary_key=True)
    mount: Mapped[str] = mapped_column(String(16), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    used_pct: Mapped[Decimal] = mapped_column(Pct)


class MetricHourly(Base):
    __tablename__ = "metrics_hourly"

    host_id: Mapped[int] = mapped_column(UInt, primary_key=True)
    hour: Mapped[datetime] = mapped_column(primary_key=True)
    cpu_avg: Mapped[Decimal] = mapped_column(Pct)
    cpu_max: Mapped[Decimal] = mapped_column(Pct)
    ram_avg: Mapped[Decimal] = mapped_column(Pct)
    ram_max: Mapped[Decimal] = mapped_column(Pct)
    latency_avg: Mapped[Decimal | None] = mapped_column(Ms)
    latency_max: Mapped[Decimal | None] = mapped_column(Ms)
    net_in_avg: Mapped[int | None] = mapped_column(UBigInt)           # tambahan: grafik trafik 7–30 hari
    net_out_avg: Mapped[int | None] = mapped_column(UBigInt)
    disk_max_pct: Mapped[Decimal | None] = mapped_column(Pct)
    online_min: Mapped[int] = mapped_column(UTinyInt)                 # menit online (laporan ketersediaan)


class Screenshot(Base):
    __tablename__ = "screenshots"
    __table_args__ = (
        Index("ix_shot_host", "host_id", "requested_at"),
        Index("ix_shot_expiry", "status", "expires_at"),
        CheckConstraint("status IN ('requested','uploaded','failed','expired')", name="ck_shot_status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True)
    request_uuid: Mapped[str] = mapped_column(CHAR(36), unique=True)
    host_id: Mapped[int] = mapped_column(UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_shot_host"))
    requested_by: Mapped[int] = mapped_column(UInt, ForeignKey("users.id", name="fk_shot_user"))
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reason: Mapped[str] = mapped_column(String(200))
    notify_user: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(10), server_default="requested")
    file_path: Mapped[str | None] = mapped_column(String(255))
    width: Mapped[int | None] = mapped_column(USmallInt)
    height: Mapped[int | None] = mapped_column(USmallInt)
    size_bytes: Mapped[int | None] = mapped_column(UInt)
    error: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime | None]


class RemoteSession(Base):
    __tablename__ = "remote_sessions"
    __table_args__ = (
        Index("ix_remote_host", "host_id", "started_at"),
        Index("ix_remote_user", "user_id", "started_at"),
        CheckConstraint("mode IN ('view','control')", name="ck_remote_mode"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True)
    host_id: Mapped[int] = mapped_column(UInt, ForeignKey("hosts.id", ondelete="CASCADE", name="fk_remote_host"))
    user_id: Mapped[int] = mapped_column(UInt, ForeignKey("users.id", name="fk_remote_user"))
    mode: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(200))
    client_ip: Mapped[str] = mapped_column(String(45))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(String(20))
