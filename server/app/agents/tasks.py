"""Kerja periodik domain agents, dijalankan worker tiap ±60 detik oleh satu worker (GET_LOCK)."""
from app.agents import service
from app.core.config import settings
from app.jobs.queue import maintenance


@maintenance
async def sync_repo_releases(ctx: dict) -> None:
    """Rilis baru dari agent/releases (setelah git pull) → terdaftar dan dimulai di Ring 0."""
    async with ctx["db"]() as db:
        await service.sync_repo_releases(db, settings.RELEASE_REPO_DIR)


@maintenance
async def auto_promote_release(ctx: dict) -> None:
    async with ctx["db"]() as db:
        await service.auto_promote(db)
