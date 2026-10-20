"""Kerja periodik domain remote (worker, tiap ±60 detik)."""
import logging

from app.jobs.queue import maintenance
from app.remote.service import expire_stale

logger = logging.getLogger(__name__)


@maintenance
async def expire_remote_sessions(ctx: dict) -> None:
    async with ctx["db"]() as db:
        if closed := await expire_stale(db):
            logger.info("remote_sessions_expired", extra={"count": closed})
