"""Seri grafik per PC. Rentang ≤ 24 jam dari metrics (range scan PK host_id, ts), 7–30 hari dari metrics_hourly.
Dirender sebagai SVG di server (tanpa library chart, aman untuk CSP)."""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.templating import MONTHS
from app.hosts.models import DiskMetric, Metric, MetricHourly

# key: (detik rentang, jumlah batang, sumber)
RANGES = {
    "1h": (3600, 60, "raw"),
    "24h": (86400, 48, "raw"),
    "7d": (7 * 86400, 42, "hourly"),
    "30d": (30 * 86400, 60, "hourly"),
}
RANGE_LABELS = {"1h": "1 jam", "24h": "24 jam", "7d": "7 hari", "30d": "30 hari"}
# Garis bantu visual saja; ambang alert sebenarnya ada di aturan alert
THRESHOLDS = {"cpu": 90, "ram": 90, "disk": 90}


@dataclass
class Series:
    values: list[float | None]
    peaks: list[float | None]
    scale: float = 100.0
    threshold: float | None = None
    current: float | None = None
    peak: float | None = None
    peak_at: datetime | None = None
    avg: float | None = None

    @property
    def bars(self) -> list[tuple[int, float, bool]]:
        """(indeks, tinggi 0–100, melewati ambang) untuk batang yang ada datanya."""
        out = []
        for i, v in enumerate(self.values):
            if v is None:
                continue
            height = max(2.0, min(100.0, v / self.scale * 100)) if self.scale else 2.0
            hot = self.threshold is not None and (self.peaks[i] or v) >= self.threshold
            out.append((i, round(height, 1), hot))
        return out

    @property
    def threshold_y(self) -> float | None:
        return None if self.threshold is None else round(100 - self.threshold / self.scale * 100, 1)


@dataclass
class Charts:
    range_key: str
    buckets: int
    axis: list[str] = field(default_factory=list)
    cpu: Series | None = None
    ram: Series | None = None
    latency: Series | None = None
    net_in: Series | None = None
    net_out: Series | None = None


def _axis(now: datetime, seconds: int) -> list[str]:
    tz = ZoneInfo(settings.DISPLAY_TIMEZONE)
    labels = []
    for i in range(5):
        t = (now - timedelta(seconds=seconds * (4 - i) / 4)).replace(tzinfo=UTC).astimezone(tz)
        if i == 4:
            labels.append("Sekarang")
        elif seconds <= 86400:
            labels.append(t.strftime("%H:%M"))
        else:
            labels.append(f"{t.day} {MONTHS[t.month - 1]}")
    return labels


def _series(rows: list, buckets: int, avg_idx: int, max_idx: int, start: datetime, bucket_sec: int,
            scale: float | None = None, threshold: float | None = None) -> Series:
    values: list[float | None] = [None] * buckets
    peaks: list[float | None] = [None] * buckets
    for row in rows:
        b = int(row[0])
        if 0 <= b < buckets and row[avg_idx] is not None:
            values[b] = float(row[avg_idx])
            peaks[b] = float(row[max_idx]) if row[max_idx] is not None else values[b]
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    s = Series(values=values, peaks=peaks, threshold=threshold)
    if present:
        peak_i = max(range(buckets), key=lambda i: peaks[i] if peaks[i] is not None else -1)
        s.peak = peaks[peak_i]
        s.peak_at = start + timedelta(seconds=bucket_sec * peak_i)
        s.avg = sum(v for _, v in present) / len(present)
        s.current = present[-1][1]
    if scale is None:   # skala otomatis (latency, trafik): puncak dibulatkan ke atas
        top = max((p for p in peaks if p is not None), default=0)
        scale = _nice_ceiling(top)
    s.scale = scale
    return s


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    for step in (10, 20, 40, 50, 100, 200, 500, 1000, 2000, 5000):
        if value <= step:
            return float(step)
    magnitude = 10 ** (len(str(int(value))) - 1)
    return float((int(value / magnitude) + 1) * magnitude)


async def host_charts(db: AsyncSession, host_id: int, range_key: str) -> Charts:
    seconds, buckets, source = RANGES.get(range_key, RANGES["24h"])
    bucket_sec = seconds // buckets
    now = await db.scalar(select(func.now()))                    # jam DB (UTC), sama untuk semua instance
    start = now - timedelta(seconds=seconds)
    charts = Charts(range_key=range_key if range_key in RANGES else "24h", buckets=buckets,
                    axis=_axis(now, seconds))

    if source == "raw":
        ts, table = Metric.ts, Metric
        cols = (func.avg(Metric.cpu_pct), func.max(Metric.cpu_pct), func.avg(Metric.ram_pct),
                func.max(Metric.ram_pct), func.avg(Metric.latency_ms), func.max(Metric.latency_ms),
                func.avg(Metric.net_in_bps), func.max(Metric.net_in_bps), func.avg(Metric.net_out_bps),
                func.max(Metric.net_out_bps))
    else:
        ts, table = MetricHourly.hour, MetricHourly
        cols = (func.avg(MetricHourly.cpu_avg), func.max(MetricHourly.cpu_max), func.avg(MetricHourly.ram_avg),
                func.max(MetricHourly.ram_max), func.avg(MetricHourly.latency_avg),
                func.max(MetricHourly.latency_max), func.avg(MetricHourly.net_in_avg),
                func.max(MetricHourly.net_in_avg), func.avg(MetricHourly.net_out_avg),
                func.max(MetricHourly.net_out_avg))
    bucket = func.floor(func.timestampdiff(literal_column("SECOND"), start, ts) / bucket_sec)
    rows = (await db.execute(
        select(bucket.label("b"), *cols).select_from(table)
        .where(table.host_id == host_id, ts >= start).group_by(bucket)
    )).all()

    charts.cpu = _series(rows, buckets, 1, 2, start, bucket_sec, scale=100, threshold=THRESHOLDS["cpu"])
    charts.ram = _series(rows, buckets, 3, 4, start, bucket_sec, scale=100)
    charts.latency = _series(rows, buckets, 5, 6, start, bucket_sec)
    charts.net_in = _series(rows, buckets, 7, 8, start, bucket_sec)
    charts.net_out = _series(rows, buckets, 9, 10, start, bucket_sec)
    return charts


@dataclass
class DiskTrend:
    mount: str
    growth_pct: float          # kenaikan pemakaian 7 hari (poin persen)
    days_to_full: float | None


async def disk_trends(db: AsyncSession, host_id: int, days: int = 7) -> dict[str, DiskTrend]:
    """Kenaikan pemakaian disk dalam N hari dan perkiraan penuh (linear), dari disk_metrics."""
    first = func.min(DiskMetric.ts)
    rows = (await db.execute(
        select(DiskMetric.mount, first, func.max(DiskMetric.ts))
        .where(DiskMetric.host_id == host_id,
               DiskMetric.ts >= func.timestampadd(literal_column("DAY"), -days, func.now()))
        .group_by(DiskMetric.mount)
    )).all()
    trends = {}
    for mount, t0, t1 in rows:
        if t0 == t1:
            continue
        values = dict((await db.execute(
            select(DiskMetric.ts, DiskMetric.used_pct)
            .where(DiskMetric.host_id == host_id, DiskMetric.mount == mount, DiskMetric.ts.in_([t0, t1]))
        )).all())
        growth = float(values[t1] - values[t0])
        span_days = (t1 - t0).total_seconds() / 86400
        rate = growth / span_days if span_days else 0
        remaining = 100 - float(values[t1])
        trends[mount] = DiskTrend(mount, growth, remaining / rate if rate > 0.01 else None)
    return trends
