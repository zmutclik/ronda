"""Payload dari agent (JSON). Panjang string dibatasi di sini: mode strict MariaDB menolak "Data too long" → 500."""
from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


def _utc_naive(value: datetime) -> datetime:
    # Kolom DATETIME menyimpan UTC tanpa zona (engine memaksa time_zone +00:00)
    return value.astimezone(UTC).replace(tzinfo=None, microsecond=0)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_utc_naive)]
Pct = Annotated[float, Field(ge=0, le=100)]


class _Payload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


class EnrollIn(_Payload):
    enrollment_key: str = Field(min_length=8, max_length=100)
    hostname: str = Field(min_length=1, max_length=63)
    agent_version: str | None = Field(default=None, max_length=20)
    ip: str | None = Field(default=None, max_length=45)
    serial_number: str | None = Field(default=None, max_length=60)


class DiskSnapshot(_Payload):
    mount: str = Field(min_length=1, max_length=16)
    used_pct: Pct


class HeartbeatIn(_Payload):
    ts: UtcDatetime
    agent_version: str | None = Field(default=None, max_length=20)
    update_status: str | None = Field(default=None, pattern="^(none|waiting|downloading|installed|rollback|failed)$")
    ip: str | None = Field(default=None, max_length=45)
    cpu_pct: Pct
    ram_pct: Pct
    disks: list[DiskSnapshot] = Field(default_factory=list, max_length=26)
    latency_ms: float | None = Field(default=None, ge=0, le=99999)
    logged_user: str | None = Field(default=None, max_length=64)
    boot_at: UtcDatetime | None = None


class MetricSample(_Payload):
    ts: UtcDatetime
    cpu_pct: Pct
    ram_pct: Pct
    latency_ms: float | None = Field(default=None, ge=0, le=99999)
    net_in_bps: int | None = Field(default=None, ge=0)
    net_out_bps: int | None = Field(default=None, ge=0)


class DiskSample(_Payload):
    ts: UtcDatetime
    mount: str = Field(min_length=1, max_length=16)
    used_pct: Pct


class MetricsIn(_Payload):
    samples: list[MetricSample] = Field(default_factory=list, max_length=500)
    disks: list[DiskSample] = Field(default_factory=list, max_length=500)


class DiskInfo(_Payload):
    mount: str = Field(min_length=1, max_length=16)
    label: str | None = Field(default=None, max_length=40)
    media: str | None = Field(default=None, max_length=10)
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)


class AdapterInfo(_Payload):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=120)
    mac: str | None = Field(default=None, max_length=17)
    is_virtual: bool = False
    is_up: bool = False
    is_server_route: bool = False
    speed_mbps: int | None = Field(default=None, ge=0, le=4_000_000)
    ssid: str | None = Field(default=None, max_length=64)
    signal_pct: int | None = Field(default=None, ge=0, le=100)
    ipv4: str | None = Field(default=None, max_length=45)
    prefix_len: int | None = Field(default=None, ge=0, le=32)
    gateway: str | None = Field(default=None, max_length=45)
    dns: str | None = Field(default=None, max_length=200)
    dhcp: bool | None = None
    rx_bps: int | None = Field(default=None, ge=0)
    tx_bps: int | None = Field(default=None, ge=0)


class InventoryIn(_Payload):
    os_name: str | None = Field(default=None, max_length=60)
    os_build: str | None = Field(default=None, max_length=30)
    cpu_model: str | None = Field(default=None, max_length=100)
    cpu_cores: int | None = Field(default=None, ge=0, le=255)
    cpu_threads: int | None = Field(default=None, ge=0, le=65535)
    ram_total_mb: int | None = Field(default=None, ge=0)
    manufacturer: str | None = Field(default=None, max_length=60)
    model: str | None = Field(default=None, max_length=80)
    serial_number: str | None = Field(default=None, max_length=60)
    disks: list[DiskInfo] = Field(default_factory=list, max_length=26)
    adapters: list[AdapterInfo] = Field(default_factory=list, max_length=32)


class AppInfo(_Payload):
    name: str = Field(min_length=1, max_length=255)
    version: str = Field(default="", max_length=60)
    publisher: str | None = Field(default=None, max_length=150)
    install_date: date | None = None


class AppsIn(_Payload):
    hash: str = Field(pattern="^[0-9a-f]{64}$")
    apps: list[AppInfo] = Field(max_length=2000)


class ScreenshotFailIn(_Payload):
    error: str = Field(min_length=1, max_length=200)
