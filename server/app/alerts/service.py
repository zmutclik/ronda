import re
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, literal_column, or_, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.alerts import rules as rule_text
from app.alerts.evaluate import notify, refresh_problems
from app.alerts.models import (
    LEVELS, METRICS, OPERATORS, Alert, AlertNote, AlertRule, AlertRuleChannel, NotificationChannel,
)
from app.auth import audit
from app.auth.models import User
from app.core.config import settings
from app.core.database import db_now_plus
from app.folders.models import Folder
from app.hosts.models import Host, HostEvent, HostLatest
from app.integrations import telegram, whatsapp
from app.settings.models import Setting
from app.settings.service import get_value

HISTORY_PAGE = 20
HISTORY_RANGES = {"today": "Hari ini", "7d": "7 hari terakhir", "30d": "30 hari"}
MUTE_HOURS = 4
QUIET_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?")


@dataclass
class AlertFilters:
    level: str | None = None
    unit_id: int | None = None
    rule_id: int | None = None
    unacked: bool = False
    range_key: str = "7d"


def _unit_filter(unit_id: int):
    folders = select(Folder.id).where((Folder.id == unit_id) | (Folder.parent_id == unit_id))
    return Host.folder_id.in_(folders)


def _apply(stmt, f: AlertFilters):
    if f.level in LEVELS:
        stmt = stmt.where(Alert.level == f.level)
    if f.unit_id:
        stmt = stmt.where(_unit_filter(f.unit_id))
    if f.rule_id:
        stmt = stmt.where(Alert.rule_id == f.rule_id)
    return stmt


Acker = aliased(User)
Resolver = aliased(User)


def _alert_select():
    return (
        select(Alert, Host.hostname, Host.display_name, Host.folder_id, AlertRule, HostLatest.disk_max_mount,
               Acker.username.label("acked_by"), Resolver.username.label("resolved_by_name"))
        .join(Host, Host.id == Alert.host_id).join(AlertRule, AlertRule.id == Alert.rule_id)
        .outerjoin(HostLatest, HostLatest.host_id == Alert.host_id)
        .outerjoin(Acker, Acker.id == Alert.acknowledged_by)
        .outerjoin(Resolver, Resolver.id == Alert.resolved_by)
    )


@dataclass
class AlertRow:
    alert: Alert
    host_name: str
    folder_id: int | None
    rule: AlertRule
    mount: str | None
    acked_by: str | None
    resolved_by: str | None

    @property
    def rule_text(self) -> str:
        return rule_text.short_rule(self.rule, self.mount)

    @property
    def value_text(self) -> str:
        return rule_text.value_text(self.rule.metric, self.alert.value)


def _rows(result) -> list[AlertRow]:
    return [AlertRow(a, display or hostname, folder_id, rule, mount, acked, resolved)
            for a, hostname, display, folder_id, rule, mount, acked, resolved in result]


async def active_alerts(db: AsyncSession, f: AlertFilters) -> list[AlertRow]:
    stmt = _apply(_alert_select().where(Alert.open_flag == 1), f).order_by(
        func.field(Alert.level, "critical", "warning", "info"),
        func.field(Alert.status, "new", "acknowledged"), Alert.started_at.desc(),
    ).limit(200)
    if f.unacked:
        stmt = stmt.where(Alert.status == "new")
    return _rows(await db.execute(stmt))


def today_start_utc() -> datetime:
    """Pukul 00:00 hari ini di zona tampilan, sebagai DATETIME UTC (batas "hari ini" untuk tampilan)."""
    midnight = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).replace(tzinfo=None)


def _range_start(range_key: str):
    if range_key == "today":
        return today_start_utc()
    days = 30 if range_key == "30d" else 7
    return db_now_plus(-days * 86400)


async def history(db: AsyncSession, f: AlertFilters, *, before_id: int | None = None):
    base = _apply(_alert_select().where(Alert.open_flag.is_(None), Alert.started_at >= _range_start(f.range_key)), f)
    total = await db.scalar(
        _apply(select(func.count()).select_from(Alert).join(Host, Host.id == Alert.host_id)
               .where(Alert.open_flag.is_(None), Alert.started_at >= _range_start(f.range_key)), f))
    stmt = base.order_by(Alert.id.desc()).limit(HISTORY_PAGE + 1)
    if before_id:
        stmt = stmt.where(Alert.id < before_id)
    rows = _rows(await db.execute(stmt))
    more = len(rows) > HISTORY_PAGE
    rows = rows[:HISTORY_PAGE]
    return rows, (rows[-1].alert.id if more else None), total


async def history_export(db: AsyncSession, f: AlertFilters, limit: int = 10000) -> list[AlertRow]:
    stmt = _apply(_alert_select().where(Alert.open_flag.is_(None), Alert.started_at >= _range_start(f.range_key)), f)
    return _rows(await db.execute(stmt.order_by(Alert.id.desc()).limit(limit)))


async def kpis(db: AsyncSession) -> dict:
    is_open = Alert.open_flag == 1
    row = (await db.execute(select(
        func.sum(func.if_(is_open & (Alert.level == "critical"), 1, 0)),
        func.sum(func.if_(is_open & (Alert.level == "warning"), 1, 0)),
        func.sum(func.if_(is_open & (Alert.status == "new"), 1, 0)),
        func.sum(func.if_(is_open, 1, 0)),
    ).where(or_(is_open, Alert.started_at >= db_now_plus(-86400))))).one()
    today = _range_start("today")
    resolved_today = await db.scalar(select(func.count()).select_from(Alert).where(Alert.resolved_at >= today))
    response = await db.scalar(
        select(func.avg(func.timestampdiff(literal_column("SECOND"), Alert.started_at, Alert.acknowledged_at)))
        .where(Alert.acknowledged_at >= db_now_plus(-7 * 86400)))
    critical, warning, unacked, open_total = (int(v or 0) for v in row)
    return {"critical": critical, "warning": warning, "unacked": unacked, "open": open_total,
            "resolved_today": resolved_today or 0, "avg_response": float(response) if response else None}


async def open_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(Alert).where(Alert.open_flag == 1))


async def recent(db: AsyncSession, limit: int = 6) -> list[AlertRow]:
    """Dashboard "Alert terbaru": dibuka maupun selesai, terbaru dulu."""
    return _rows(await db.execute(_alert_select().order_by(Alert.id.desc()).limit(limit)))


# ---------- Aksi ----------

class AlertError(ValueError):
    pass


async def _load_open(db: AsyncSession, ids: list[int]) -> list[tuple[Alert, str]]:
    return (await db.execute(
        select(Alert, Host.hostname).join(Host, Host.id == Alert.host_id)
        .where(Alert.id.in_(ids), Alert.open_flag == 1).with_for_update(of=Alert)
    )).all()


async def acknowledge(db: AsyncSession, ids: list[int], actor: audit.Actor, note: str | None = None) -> int:
    if note and len(note) > 500:
        raise AlertError("Catatan maksimal 500 karakter.")
    async with db.begin():
        rows = [(a, h) for a, h in await _load_open(db, ids) if a.status == "new"]
        for alert, hostname in rows:
            alert.status, alert.acknowledged_by, alert.acknowledged_at = "acknowledged", actor.user_id, func.now()
            if note:
                db.add(AlertNote(alert_id=alert.id, user_id=actor.user_id, note=note))
            await audit.record(db, actor, "alert", "Akui alert", target_type="host", target_id=alert.host_id,
                               target_label=hostname,
                               detail=alert.message + (f" · catatan: {note}" if note else ""))
    return len(rows)


async def mute(db: AsyncSession, ids: list[int], actor: audit.Actor, hours: int = MUTE_HOURS) -> int:
    async with db.begin():
        rows = await _load_open(db, ids)
        for alert, hostname in rows:
            alert.muted_until = db_now_plus(hours * 3600)
            await audit.record(db, actor, "alert", "Bisukan alert", target_type="host", target_id=alert.host_id,
                               target_label=hostname, detail=f"{alert.message} · {hours} jam")
    return len(rows)


async def resolve(db: AsyncSession, ids: list[int], actor: audit.Actor, note: str | None = None) -> int:
    note = (note or "").strip()[:150] or None
    async with db.begin():
        rows = await _load_open(db, ids)
        for alert, hostname in rows:
            alert.status, alert.resolved_at, alert.resolved_by = "resolved", func.now(), actor.user_id
            alert.resolve_note = f"{actor.username}" + (f" · {note}" if note else "")
            db.add(HostEvent(host_id=alert.host_id, kind="alert", level="ok",
                             message=f"Alert diselesaikan {actor.username}: {alert.message}"[:255]))
            rule = await db.get(AlertRule, alert.rule_id)
            if rule and rule.notify_resolved:
                notify(db, alert.id, list(await db.scalars(
                    select(AlertRuleChannel.channel_id).where(AlertRuleChannel.rule_id == rule.id))), "resolved")
            await audit.record(db, actor, "alert", "Selesaikan alert", target_type="host",
                               target_id=alert.host_id, target_label=hostname,
                               detail=alert.message + (f" · {note}" if note else ""))
        await refresh_problems(db, {a.host_id for a, _ in rows})
    return len(rows)


# ---------- Aturan & kanal ----------

@dataclass
class RuleRow:
    rule: AlertRule
    firing: int
    channels: list[NotificationChannel]

    @property
    def description(self) -> str:
        return rule_text.describe(self.rule)


async def rules(db: AsyncSession) -> list[RuleRow]:
    firing = dict((await db.execute(
        select(Alert.rule_id, func.count()).where(Alert.open_flag == 1).group_by(Alert.rule_id))).all())
    links = (await db.execute(
        select(AlertRuleChannel.rule_id, NotificationChannel)
        .join(NotificationChannel, NotificationChannel.id == AlertRuleChannel.channel_id))).all()
    by_rule: dict[int, list] = {}
    for rule_id, channel in links:
        by_rule.setdefault(rule_id, []).append(channel)
    return [RuleRow(r, firing.get(r.id, 0), by_rule.get(r.id, []))
            for r in await db.scalars(select(AlertRule).order_by(AlertRule.id))]


async def rule_options(db: AsyncSession) -> list[tuple[int, str]]:
    return list((await db.execute(select(AlertRule.id, AlertRule.name).order_by(AlertRule.id))).all())


async def channels(db: AsyncSession) -> list[NotificationChannel]:
    return list(await db.scalars(select(NotificationChannel).order_by(NotificationChannel.id)))


@dataclass
class RuleInput:
    name: str
    metric: str
    operator: str
    threshold: Decimal
    duration_sec: int
    level: str
    folder_id: int | None
    notify_resolved: bool
    channel_ids: list[int]


def parse_rule(form) -> RuleInput:
    name = str(form.get("name", "")).strip()
    if not 1 <= len(name) <= 60:
        raise AlertError("Nama aturan 1–60 karakter.")
    metric, op, level = str(form.get("metric", "")), str(form.get("operator", "")), str(form.get("level", ""))
    if metric not in METRICS or op not in OPERATORS or level not in LEVELS:
        raise AlertError("Metrik, operator, atau level tidak dikenal.")
    try:
        threshold = Decimal(str(form.get("threshold", "")).replace(",", "."))
        minutes = int(form.get("duration_min") or 0)
        folder_id = int(form.get("folder_id") or 0) or None
        channel_ids = [int(c) for c in form.getlist("channel_id")]
    except (InvalidOperation, ValueError) as err:
        raise AlertError("Ambang dan durasi harus angka.") from err
    if not Decimal(0) <= threshold < Decimal(100_000_000) or not 0 <= minutes <= 1440:
        raise AlertError("Ambang atau durasi di luar batas.")
    return RuleInput(name, metric, op, threshold.quantize(Decimal("0.01")), minutes * 60, level, folder_id,
                     form.get("notify_resolved") == "1", channel_ids)


async def save_rule(db: AsyncSession, rule_id: int | None, data: RuleInput, actor: audit.Actor) -> AlertRule:
    async with db.begin():
        if data.folder_id and await db.get(Folder, data.folder_id) is None:
            raise AlertError("Folder tidak ditemukan.")
        if rule_id is None:
            rule = AlertRule(updated_by=actor.user_id)
            db.add(rule)
        else:
            rule = await db.get(AlertRule, rule_id, with_for_update=True)
            if rule is None:
                raise AlertError("Aturan tidak ditemukan.")
        for key in ("name", "metric", "operator", "threshold", "duration_sec", "level", "folder_id",
                    "notify_resolved"):
            setattr(rule, key, getattr(data, key))
        rule.updated_by = actor.user_id
        await db.flush()
        await db.execute(delete(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule.id))
        valid = list(await db.scalars(select(NotificationChannel.id).where(
            NotificationChannel.id.in_(data.channel_ids))))
        if valid:
            await db.execute(insert(AlertRuleChannel).values([{"rule_id": rule.id, "channel_id": c} for c in valid]))
        await audit.record(db, actor, "alert", "Tambah aturan" if rule_id is None else "Ubah aturan",
                           target_type="rule", target_id=rule.id, target_label=data.name,
                           detail=rule_text.describe(rule))
        await db.refresh(rule)
    return rule


async def toggle_rule(db: AsyncSession, rule_id: int, active: bool, actor: audit.Actor) -> None:
    """Menonaktifkan aturan menyelesaikan alert terbukanya, agar tidak menggantung selamanya."""
    async with db.begin():
        rule = await db.get(AlertRule, rule_id, with_for_update=True)
        if rule is None:
            raise AlertError("Aturan tidak ditemukan.")
        rule.is_active, rule.updated_by = active, actor.user_id
        if not active:
            hosts = set(await db.scalars(select(Alert.host_id).where(Alert.rule_id == rule_id, Alert.open_flag == 1)))
            await db.execute(update(Alert).where(Alert.rule_id == rule_id, Alert.open_flag == 1).values(
                status="resolved", resolved_at=func.now(), resolved_by=actor.user_id,
                resolve_note="Aturan dinonaktifkan"))
            await refresh_problems(db, hosts)
        await audit.record(db, actor, "alert", "Aktifkan aturan" if active else "Nonaktifkan aturan",
                           target_type="rule", target_id=rule_id, target_label=rule.name)


def _validate_channel(kind: str, name: str, target: str) -> None:
    if kind not in ("telegram", "whatsapp"):
        raise AlertError("Jenis kanal tidak dikenal.")
    if not 1 <= len(name) <= 60 or not 1 <= len(target) <= 100:
        raise AlertError("Nama kanal 1–60 karakter, tujuan 1–100 karakter.")


async def add_channel(db: AsyncSession, kind: str, name: str, target: str, actor: audit.Actor) -> None:
    _validate_channel(kind, name, target)
    async with db.begin():
        channel = NotificationChannel(kind=kind, name=name, target=target)
        db.add(channel)
        await db.flush()
        await audit.record(db, actor, "alert", "Tambah kanal notifikasi", target_type="channel",
                           target_id=channel.id, target_label=name, detail=kind)


async def remove_channel(db: AsyncSession, channel_id: int, actor: audit.Actor) -> None:
    async with db.begin():
        channel = await db.get(NotificationChannel, channel_id)
        if channel is None:
            raise AlertError("Kanal tidak ditemukan.")
        await db.delete(channel)
        await audit.record(db, actor, "alert", "Hapus kanal notifikasi", target_type="channel",
                           target_id=channel_id, target_label=channel.name)


async def get_channel(db: AsyncSession, channel_id: int) -> NotificationChannel | None:
    return await db.get(NotificationChannel, channel_id)


async def send_to_channel(client: httpx.AsyncClient, channel: NotificationChannel, text: str) -> None:
    """Kirim satu pesan ke kanal. Dipanggil di luar transaksi (I/O pihak ketiga)."""
    if channel.kind == "telegram":
        await telegram.send_message(client, settings.TELEGRAM_BOT_TOKEN, channel.target, text)
    else:
        await whatsapp.send_message(client, settings.WHATSAPP_API_URL, settings.WHATSAPP_API_USER,
                                    settings.WHATSAPP_API_PASSWORD, channel.target, text)


async def quiet_hours(db: AsyncSession) -> dict:
    return await get_value(db, "quiet_hours")


async def save_quiet_hours(db: AsyncSession, start: str, end: str, critical_still_sent: bool,
                           actor: audit.Actor) -> None:
    # Yang divalidasi = yang disimpan. Dulu fromisoformat(teks mentah) menerima "220000"/"T22:00", lalu potongan
    # [:5]-nya ("22000") tersimpan dan in_quiet_hours gagal di setiap job notifikasi → semua alert tidak terkirim.
    # Input jam di browser mengirim HH:MM (atau HH:MM:SS bila ada step detik).
    if not (QUIET_TIME.fullmatch(start) and QUIET_TIME.fullmatch(end)):
        raise AlertError("Jam harus format HH:MM.")
    value = {"start": start[:5], "end": end[:5], "critical_still_sent": critical_still_sent}
    async with db.begin():
        stmt = insert(Setting).values(name="quiet_hours", value=value)
        await db.execute(stmt.on_duplicate_key_update(value=stmt.inserted.value, updated_at=func.now()))
        await audit.record(db, actor, "alert", "Ubah jam tenang",
                           detail=f"{value['start']}–{value['end']}, kritis tetap dikirim: "
                                  f"{'ya' if critical_still_sent else 'tidak'}")


def in_quiet_hours(config: dict | None, now: datetime) -> bool:
    """now = waktu lokal (zona tampilan). Rentang boleh melewati tengah malam (22:00–06:00)."""
    if not config:
        return False
    start, end, t = time.fromisoformat(config["start"]), time.fromisoformat(config["end"]), now.time()
    return (start <= t < end) if start < end else (t >= start or t < end)


def local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.DISPLAY_TIMEZONE))

