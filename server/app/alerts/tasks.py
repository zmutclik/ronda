import logging

from sqlalchemy import func, select, update

from app.alerts import service as alert_service
from app.alerts.evaluate import evaluate_all
from app.alerts.models import LEVELS, Alert, NotificationChannel
from app.core.templating import shortdt
from app.hosts.models import Host
from app.jobs.queue import maintenance, task

logger = logging.getLogger(__name__)

ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


@maintenance
async def evaluate_alert_rules(ctx: dict) -> None:
    await evaluate_all(ctx["db"])


def format_message(alert: Alert, host_name: str, event: str) -> str:
    if event == "resolved":
        note = f" · {alert.resolve_note}" if alert.resolve_note else ""
        return f"✅ SELESAI · {host_name}\n{alert.message}{note}"
    return (f"{ICON.get(alert.level, '')} {LEVELS[alert.level].upper()} · {host_name}\n{alert.message}\n"
            f"Mulai {shortdt(alert.started_at)} WIB")


@task("alerts.notify")
async def notify(ctx: dict, alert_id: int, channel_id: int, event: str) -> None:
    """Kirim satu notifikasi ke satu kanal. Idempotent sejauh mungkin: alert yang sudah selesai tidak
    dikirim sebagai 'open', alert yang dibisukan dan peringatan di jam tenang dilewati."""
    async with ctx["db"]() as db, db.begin():
        row = (await db.execute(
            select(Alert, Host.hostname, Host.display_name, (Alert.muted_until > func.now()).label("muted"))
            .join(Host, Host.id == Alert.host_id).where(Alert.id == alert_id)
        )).one_or_none()
        channel = await db.get(NotificationChannel, channel_id)
        quiet = await alert_service.quiet_hours(db)
    if row is None or channel is None or not channel.is_active:
        return
    alert, hostname, display, muted = row
    if event == "open" and (alert.open_flag is None or muted):
        return
    if alert_service.in_quiet_hours(quiet, alert_service.local_now()):
        if alert.level != "critical" or not quiet.get("critical_still_sent", True):
            logger.info("notify_skipped_quiet_hours", extra={"alert_id": alert_id})
            return
    await alert_service.send_to_channel(ctx["http"], channel, format_message(alert, display or hostname, event))
    if event == "open":
        async with ctx["db"]() as db, db.begin():
            await db.execute(update(Alert).where(Alert.id == alert_id, Alert.notified_at.is_(None))
                             .values(notified_at=func.now()))
