from fastapi import APIRouter, Depends, Request

from app.agents import service as agent_service
from app.alerts import service as alert_service
from app.auth.dependencies import CurrentUserDep, require_permission
from app.core.database import DbSession
from app.core.templating import render, templates
from app.folders.service import load_tree
from app.hosts import service as host_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_permission("view"))])


async def _context(db) -> dict:
    threshold = await host_service.offline_after(db)
    release = await agent_service.current_release(db)
    versions = await host_service.version_counts(db)
    return {
        "counts": await host_service.status_counts(db, threshold),
        "problems": await host_service.problem_hosts(db, threshold),
        "recent": await alert_service.recent(db),
        "tree": await load_tree(db),
        "versions": versions,
        "version_segments": list(zip([n for _, n in versions[:3]], ("s-ok", "s-dl", "s-wait"), strict=False)),
        "updates": await host_service.update_status_counts(db),
        "release": release,
    }


@router.get("")
async def dashboard(request: Request, user: CurrentUserDep, db: DbSession):
    # Fragment _live.html juga dipakai polling tiap 30 detik (hx-trigger="every 30s")
    return render(request, "dashboard/index.html", "dashboard/_live.html", await _context(db))


@router.get("/nav-counts")
async def nav_counts(request: Request, user: CurrentUserDep, db: DbSession):
    """Angka di sidebar, dikirim sebagai swap out-of-band (layout.html memanggilnya tiap 60 detik)."""
    threshold = await host_service.offline_after(db)
    counts = await host_service.status_counts(db, threshold)
    return templates.TemplateResponse(request=request, name="dashboard/_nav_counts.html", context={
        "hosts": counts["total"], "pending": counts["pending"], "alerts": await alert_service.open_count(db)})
