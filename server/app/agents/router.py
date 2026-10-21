"""Dua router: API untuk agent (/api/agent, token Bearer, balasan JSON) dan halaman Pendaftaran Agent (/agents)."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.agents import service as agent_service
from app.agents.dependencies import AgentDep, ApprovedAgentDep
from app.agents.models import RING_LABELS
from app.auth.dependencies import CurrentUserDep, require_csrf, require_permission
from app.core.config import settings
from app.core.database import DbSession
from app.core.dependencies import client_ip
from app.core.templating import render, templates
from app.folders.service import load_tree
from app.hosts import ingest
from app.hosts import service as host_service
from app.hosts.schemas import AppsIn, EnrollIn, HeartbeatIn, InventoryIn, MetricsIn, ScreenshotFailIn

api = APIRouter(prefix="/api/agent", tags=["agent-api"])


@api.post("/enroll")
async def enroll(data: EnrollIn, request: Request, db: DbSession):
    try:
        host_id, token, status = await agent_service.enroll(db, data, client_ip(request))
    except agent_service.AgentError as err:
        raise HTTPException(status_code=403, detail=str(err)) from err
    return {"host_id": host_id, "agent_token": token, "status": status}


@api.post("/heartbeat")
async def heartbeat(data: HeartbeatIn, agent: AgentDep, request: Request, db: DbSession):
    return await ingest.heartbeat(db, agent.id, data, client_ip(request))


@api.post("/metrics")
async def metrics(data: MetricsIn, agent: ApprovedAgentDep, db: DbSession):
    return {"stored": await ingest.store_metrics(db, agent.id, data)}


@api.put("/inventory")
async def inventory(data: InventoryIn, agent: ApprovedAgentDep, db: DbSession):
    await ingest.store_inventory(db, agent.id, data)
    return {"ok": True}


@api.put("/apps")
async def apps(data: AppsIn, agent: ApprovedAgentDep, db: DbSession):
    return {"changed": await ingest.store_apps(db, agent.id, data)}


async def _read_limited(request: Request, limit: int) -> bytes:
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(status_code=413, detail="File terlalu besar.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="File terlalu besar.")
    return bytes(body)


@api.post("/screenshots/{request_id}")
async def upload_screenshot(request_id: str, request: Request, agent: ApprovedAgentDep, db: DbSession):
    """Body = gambar (JPEG/PNG/WebP) apa adanya; header X-Width / X-Height opsional."""
    content = await _read_limited(request, settings.SCREENSHOT_MAX_BYTES)
    try:
        width = int(request.headers.get("x-width") or 0) or None
        height = int(request.headers.get("x-height") or 0) or None
        await ingest.store_screenshot(db, agent.id, request_id[:36], content, width, height)
    except ValueError as err:          # ScreenshotRejected, atau header ukuran bukan angka
        raise HTTPException(status_code=422, detail=str(err)) from err
    return {"ok": True}


@api.post("/screenshots/{request_id}/fail")
async def screenshot_failed(request_id: str, data: ScreenshotFailIn, agent: ApprovedAgentDep, db: DbSession):
    if not await ingest.fail_screenshot(db, agent.id, request_id[:36], data.error):
        raise HTTPException(status_code=404, detail="Permintaan screenshot tidak ditemukan.")
    return {"ok": True}


@api.get("/update")
async def update_check(agent: ApprovedAgentDep, db: DbSession):
    release = await agent_service.update_for_host(db, agent.ring, agent.agent_version)
    if release is None:
        return {"update": None}
    return {"update": {
        "version": release.version, "sha256": release.sha256, "signature": release.signature,
        "size": release.size_bytes, "url": f"/api/agent/releases/{release.version}/download",
    }}


def _release_file(file_path: str):
    root = settings.RELEASE_DIR.resolve()
    target = (root / file_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404)
    return target


@api.get("/releases/{version}/download")
async def download_release(version: str, agent: ApprovedAgentDep, db: DbSession):
    release = await agent_service.get_release_by_version(db, version)
    if release is None or release.status not in ("rolling", "paused", "completed"):
        raise HTTPException(status_code=404)
    return FileResponse(_release_file(release.file_path), media_type="application/zip",
                        filename=f"ronda-agent-{release.version}.zip")


# ---------- Halaman Pendaftaran Agent ----------

pages = APIRouter(prefix="/agents", tags=["agents"],
                  dependencies=[Depends(require_csrf), Depends(require_permission("agent_approve"))])


async def _hub_context(db, request: Request) -> dict:
    user = request.state.user              # kartu key & rilis hanya dimuat untuk yang berhak (lihat _hub.html)
    tree = await load_tree(db)
    can_release = user.can("agent_release")
    release = await agent_service.current_release(db) if can_release else None
    return {
        "tree": tree,
        "pending": await agent_service.pending_hosts(db, tree),
        "keys": await agent_service.active_keys(db) if user.can("agent_enroll") else [],
        "release": release,
        "ring_stats": await agent_service.ring_stats(db, release) if release else [],
        "releases": await agent_service.releases(db) if can_release else [],
        "versions": await host_service.version_counts(db),
        "ring_labels": RING_LABELS,
        "server_url": str(request.base_url).rstrip("/"),
    }


@pages.get("")
async def hub(request: Request, user: CurrentUserDep, db: DbSession):
    context = await _hub_context(db, request)
    return render(request, "agents/hub.html", "agents/_hub.html", context)


def _hub_refresh(request: Request, context: dict, message: str | None = None, new_key: str | None = None):
    headers = {"HX-Trigger": json.dumps({"toast": message})} if message else {}
    return templates.TemplateResponse(request=request, name="agents/_hub.html",
                                      context={**context, "new_key": new_key}, headers=headers)


@pages.post("/approve")
async def approve(request: Request, user: CurrentUserDep, db: DbSession):
    """Setujui PC terpilih. Field folder_<id> berisi folder tujuan ('' = belum dikelompokkan)."""
    form = await request.form()
    try:
        ids = [int(v) for v in form.getlist("host_id")]
        choices = {i: int(form.get(f"folder_{i}") or 0) or None for i in ids}
    except ValueError as err:
        raise HTTPException(status_code=400) from err
    async with db.begin():                 # bacaan selesai sebelum service membuka transaksinya
        tree = await load_tree(db)
    try:
        count = await agent_service.approve_hosts(db, choices, tree, user.actor)
    except agent_service.AgentError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return _hub_refresh(request, await _hub_context(db, request), f"{count} PC disetujui.")


@pages.post("/hosts/{host_id}/reject")
async def reject(host_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    try:
        await agent_service.reject_host(db, host_id, user.actor)
    except agent_service.AgentError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return _hub_refresh(request, await _hub_context(db, request), "PC ditolak.")


@pages.post("/keys", dependencies=[Depends(require_permission("agent_enroll"))])
async def create_key(request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    try:
        days = int(form.get("valid_days") or 0) or None
        _, raw = await agent_service.create_enrollment_key(
            db, prefix=str(form.get("prefix", "")), valid_days=days, actor=user.actor)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err) or "Masa berlaku harus angka.") from err
    return _hub_refresh(request, await _hub_context(db, request), new_key=raw)


@pages.post("/keys/{key_id}/revoke", dependencies=[Depends(require_permission("agent_enroll"))])
async def revoke_key(key_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    try:
        await agent_service.revoke_enrollment_key(db, key_id, user.actor)
    except agent_service.AgentError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return _hub_refresh(request, await _hub_context(db, request), "Enrollment key dicabut.")


release_pages = APIRouter(prefix="/agents/releases", tags=["agents"],
                          dependencies=[Depends(require_csrf), Depends(require_permission("agent_release"))])


@release_pages.post("")
async def upload_release(request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    package = form.get("package")
    if package is None or isinstance(package, str):
        raise HTTPException(status_code=422, detail="Pilih file paket (.zip).")
    try:
        release = await agent_service.upload_release(
            db, version=str(form.get("version", "")).strip(), signature=str(form.get("signature", "")),
            notes=str(form.get("notes", "")).strip() or None, upload=package.file, actor=user.actor)
    except agent_service.AgentError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    finally:
        await package.close()
    return _hub_refresh(request, await _hub_context(db, request), f"Rilis v{release.version} diunggah.")


@release_pages.post("/{release_id}/{action}")
async def release_action(release_id: int, action: str, request: Request, user: CurrentUserDep, db: DbSession):
    try:
        release = await agent_service.change_release(db, release_id, action, user.actor)
    except agent_service.AgentError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return _hub_refresh(request, await _hub_context(db, request), f"v{release.version}: {release.status}.")


@release_pages.get("/{release_id}/download")
async def admin_download(release_id: int, user: CurrentUserDep, db: DbSession):
    release = await agent_service.get_release(db, release_id)
    if release is None:
        raise HTTPException(status_code=404)
    return FileResponse(_release_file(release.file_path), media_type="application/zip",
                        filename=f"ronda-agent-{release.version}.zip")


router = APIRouter()
router.include_router(api)
router.include_router(release_pages)
router.include_router(pages)

