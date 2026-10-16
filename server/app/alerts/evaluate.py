"""Evaluasi aturan alert terhadap kondisi PC. Dijalankan worker tiap ±60 detik (alerts/tasks.py).
Satu transaksi pendek per aturan; notifikasi di-enqueue di transaksi yang sama (outbox, addon jobs)."""
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.alerts import rules as rule_text
from app.alerts.models import Alert, AlertRule, AlertRuleChannel
from app.core.database import db_now_plus
from app.folders.models import Folder
from app.hosts.models import Host, HostEvent, HostLatest, Metric
from app.hosts.service import APPROVED
from app.jobs.queue import enqueue
from app.settings.service import get_value

logger = logging.getLogger(__name__)

EVENT_LEVEL = {"critical": "crit", "warning": "warn", "info": None}
EVENT_LABEL = {"critical": "Alert kritis", "warning": "Peringatan", "info": "Info"}
MIN_WINDOW = 60


def stored_value(value: float | None) -> Decimal | None:
    """Nilai seperti yang tersimpan di alerts.value (DECIMAL(10,2)), agar perbandingan dengan baris DB tepat
    dan MariaDB tidak memotong desimal (peringatan "Data truncated")."""
    return None if value is None else Decimal(f"{value:.2f}")


@dataclass
class Reading:
    value: float | None
    low: float | None
    high: float | None
    mount: str | None = None


async def _scope_host_ids(db: AsyncSession, rule: AlertRule) -> list[int] | None:
    """None = semua PC; aturan per folder berlaku untuk folder itu dan ruangan di bawahnya."""
    if rule.folder_id is None:
        return None
    folders = select(Folder.id).where((Folder.id == rule.folder_id) | (Folder.parent_id == rule.folder_id))
    return list(await db.scalars(select(Host.id).where(Host.folder_id.in_(folders))))


async def readings(db: AsyncSession, rule: AlertRule, threshold: int) -> dict[int, Reading]:
    """Nilai per PC yang relevan untuk aturan ini. PC offline hanya dinilai oleh aturan offline/rollback:
    data CPU/RAM/disk-nya basi, jadi alert-nya dibiarkan apa adanya."""
    online = Host.last_seen_at > db_now_plus(-threshold)
    base = select(Host.id).where(Host.status.in_(APPROVED))
    scope = await _scope_host_ids(db, rule)
    if scope is not None:
        base = base.where(Host.id.in_(scope))

    if rule.metric == "offline_min":
        minutes = func.timestampdiff(literal_column("SECOND"), Host.last_seen_at, func.now()) / 60
        rows = await db.execute(base.add_columns(minutes).where(Host.last_seen_at.is_not(None)))
        return {h: Reading(float(m), float(m), float(m)) for h, m in rows}
    if rule.metric == "agent_rollback":
        rows = await db.execute(base.add_columns(Host.update_status.in_(("rollback", "failed"))))
        return {h: Reading(float(v), float(v), float(v)) for h, v in rows}

    base = base.where(online).join(HostLatest, HostLatest.host_id == Host.id)
    if rule.metric == "disk_pct":
        rows = await db.execute(base.add_columns(HostLatest.disk_max_pct, HostLatest.disk_max_mount))
        return {h: Reading(float(v), float(v), float(v), mount) for h, v, mount in rows}
    if rule.metric == "uptime_days":
        days = func.timestampdiff(literal_column("SECOND"), HostLatest.boot_at, func.now()) / 86400
        rows = await db.execute(base.add_columns(days).where(HostLatest.boot_at.is_not(None)))
        return {h: Reading(float(d), float(d), float(d)) for h, d in rows}

    # CPU / RAM / latency: nilai sekarang dari host_latest, min/max sepanjang jendela dari metrics
    current_col = getattr(HostLatest, rule_text.METRIC_COLUMN[rule.metric])
    current = {h: (float(v) if v is not None else None) for h, v in await db.execute(base.add_columns(current_col))}
    if not current:
        return {}
    if not rule.duration_sec:
        return {h: Reading(v, v, v) for h, v in current.items()}
    col = getattr(Metric, rule_text.METRIC_COLUMN[rule.metric])
    window = max(rule.duration_sec, MIN_WINDOW)
    rows = await db.execute(
        select(Metric.host_id, func.min(col), func.max(col))
        .where(Metric.host_id.in_(list(current)), Metric.ts >= db_now_plus(-window))   # range scan PK per PC
        .group_by(Metric.host_id)
    )
    return {h: Reading(current[h], float(lo) if lo is not None else None, float(hi) if hi is not None else None)
            for h, lo, hi in rows}


async def refresh_problems(db: AsyncSession, host_ids: set[int]) -> None:
    """Tulis ringkasan alert terbuka paling parah ke hosts.problem_* (dibaca daftar Komputer & Dashboard)."""
    await db.flush()          # autoflush mati: perubahan alert (open_flag dihitung DB) harus terkirim dulu
    for host_id in host_ids:
        rows = (await db.execute(
            select(Alert.level, Alert.value, AlertRule)
            .join(AlertRule, AlertRule.id == Alert.rule_id)
            .where(Alert.host_id == host_id, Alert.open_flag == 1)
            .order_by(func.field(Alert.level, "critical", "warning", "info"), Alert.started_at.desc())
        )).all()
        if rows:
            level, value, rule = rows[0]
            mount = None
            if rule.metric == "disk_pct":
                mount = await db.scalar(select(HostLatest.disk_max_mount).where(HostLatest.host_id == host_id))
            text = rule_text.problem_text(rule, float(value) if value is not None else None, mount)
            values = {"problem_level": level, "problem_text": text[:80], "problem_count": min(len(rows), 255)}
        else:
            values = {"problem_level": None, "problem_text": None, "problem_count": 0}
        await db.execute(update(Host).where(Host.id == host_id).values(**values))


async def _channel_ids(db: AsyncSession, rule_id: int) -> list[int]:
    return list(await db.scalars(select(AlertRuleChannel.channel_id).where(AlertRuleChannel.rule_id == rule_id)))


def notify(db: AsyncSession, alert_id: int, channel_ids: list[int], event: str) -> None:
    for channel_id in channel_ids:   # satu job per kanal: retry tidak mengirim ulang ke kanal yang sudah sukses
        enqueue(db, "alerts.notify", alert_id=alert_id, channel_id=channel_id, event=event, max_attempts=6)


async def evaluate_rule(db: AsyncSession, rule: AlertRule, threshold: int) -> tuple[int, int]:
    """Buka alert baru dan selesaikan yang sudah normal untuk satu aturan. Kembalikan (dibuka, diselesaikan)."""
    data = await readings(db, rule, threshold)
    open_alerts = {a.host_id: a for a in await db.scalars(
        select(Alert).where(Alert.rule_id == rule.id, Alert.open_flag == 1).with_for_update())}
    channels = await _channel_ids(db, rule.id)
    touched: set[int] = set()
    opened = resolved = 0
    for host_id, r in data.items():
        violated, normal = rule_text.check(rule, r.low, r.high)
        alert = open_alerts.get(host_id)
        if violated and alert is None:
            msg = rule_text.message(rule, r.value, r.mount)
            alert = Alert(host_id=host_id, rule_id=rule.id, level=rule.level, value=stored_value(r.value), message=msg[:255],
                          started_at=func.now())
            db.add(alert)
            await db.flush()
            db.add(HostEvent(host_id=host_id, kind="alert", level=EVENT_LEVEL[rule.level],
                             message=f"{EVENT_LABEL[rule.level]}: {msg}"[:255], meta={"alert_id": alert.id}))
            notify(db, alert.id, channels, "open")
            touched.add(host_id)
            opened += 1
        elif normal and alert is not None:
            note = "Otomatis (kembali online)" if rule.metric == "offline_min" else "Otomatis (kembali normal)"
            alert.resolved_at, alert.status, alert.resolve_note = func.now(), "resolved", note
            db.add(HostEvent(host_id=host_id, kind="alert", level="ok",
                             message=f"Alert selesai: {alert.message}"[:255], meta={"alert_id": alert.id}))
            if rule.notify_resolved:
                notify(db, alert.id, channels, "resolved")
            touched.add(host_id)
            resolved += 1
        elif alert is not None and r.value is not None and alert.value != stored_value(r.value):
            alert.value = stored_value(r.value)        # kolom "Nilai" tetap terkini
            touched.add(host_id)
    await refresh_problems(db, touched)
    return opened, resolved


async def evaluate_all(db_factory: async_sessionmaker) -> None:
    async with db_factory() as db, db.begin():
        threshold = int(await get_value(db, "offline_after_sec"))
        rule_ids = list(await db.scalars(select(AlertRule.id).where(AlertRule.is_active.is_(True))))
    for rule_id in rule_ids:
        try:
            async with db_factory() as db, db.begin():
                rule = await db.get(AlertRule, rule_id)
                if rule is None or not rule.is_active:
                    continue
                opened, resolved = await evaluate_rule(db, rule, threshold)
            if opened or resolved:
                logger.info("alerts_evaluated", extra={"rule_id": rule_id, "opened": opened, "resolved": resolved})
        except Exception:
            logger.exception("alert_rule_failed", extra={"rule_id": rule_id})   # aturan lain tetap dievaluasi

