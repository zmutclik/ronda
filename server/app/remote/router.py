"""Halaman sesi remote (/remote) dan laporan gagal dari agent (/api/agent/remote). WebSocket-nya di relay.py."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.agents.dependencies import ApprovedAgentDep
from app.auth.dependencies import CurrentUser, CurrentUserDep, require_csrf, require_permission
from app.core.config import settings
from app.core.database import DbSession
from app.core.templating import templates
from app.hosts import service as host_service
from app.remote import service
from app.remote.service import END_REASONS, MODES

pages = APIRouter(prefix="/remote", tags=["remote"],
                  dependencies=[Depends(require_csrf), Depends(require_permission("remote_view"))])


async def _own_session(db, session_id: int, user: CurrentUser) -> service.SessionView:
    """Hanya pembuka sesi yang boleh melihat layarnya (relay juga memeriksa ini)."""
    view = await service.get_session(db, session_id)
    if view is None or view.session.user_id != user.id:
        raise HTTPException(status_code=404)
    return view


@pages.post("/hosts/{host_id}")
async def start(host_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    mode = str(form.get("mode", "view"))
    if mode == "control" and not user.can("remote_control"):
        raise HTTPException(status_code=403, detail="Kontrol penuh hanya untuk Admin.")
    async with db.begin():                 # bacaan selesai sebelum service membuka transaksinya
        threshold = await host_service.offline_after(db)
    try:
        session = await service.start_session(db, host_id, mode=mode, reason=str(form.get("reason", "")).strip(),
                                              actor=user.actor, threshold=threshold)
    except service.RemoteError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return Response(status_code=204, headers={"HX-Redirect": f"/remote/{session.id}"})


def _context(view: service.SessionView) -> dict:
    return {"v": view, "s": view.session, "modes": MODES, "end_reasons": END_REASONS,
            "max_minutes": settings.REMOTE_MAX_DURATION // 60}


@pages.get("/{session_id}")
async def viewer(request: Request, session_id: int, user: CurrentUserDep, db: DbSession):
    view = await _own_session(db, session_id, user)
    return templates.TemplateResponse(request=request, name="remote/viewer.html", context=_context(view))


@pages.get("/{session_id}/status")
async def status(request: Request, session_id: int, user: CurrentUserDep, db: DbSession):
    view = await _own_session(db, session_id, user)
    return templates.TemplateResponse(request=request, name="remote/_status.html", context=_context(view))


@pages.post("/{session_id}/stop")
async def stop(request: Request, session_id: int, user: CurrentUserDep, db: DbSession):
    async with db.begin():
        await _own_session(db, session_id, user)
    stopped = await service.stop_session(db, session_id, user.actor)
    view = await service.get_session(db, session_id)
    headers = {"HX-Trigger": json.dumps({"toast": "Sesi remote diakhiri."})} if stopped else {}
    return templates.TemplateResponse(request=request, name="remote/_status.html", context=_context(view),
                                      headers=headers)


# ---------- API agent ----------

api = APIRouter(prefix="/api/agent/remote", tags=["agent-api"])


class RemoteFailIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error: str = Field(min_length=1, max_length=200)


@api.post("/{session_id}/fail")
async def agent_failed(session_id: int, data: RemoteFailIn, agent: ApprovedAgentDep, db: DbSession):
    if not await service.agent_failed(db, agent.id, session_id, data.error):
        raise HTTPException(status_code=404, detail="Sesi remote tidak ditemukan atau sudah selesai.")
    return {"ok": True}


router = APIRouter()
router.include_router(api)
router.include_router(pages)
