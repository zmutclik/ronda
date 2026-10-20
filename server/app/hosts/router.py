import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.auth.dependencies import CurrentUserDep, require_csrf, require_permission
from app.core.config import settings
from app.core.database import DbSession
from app.core.templating import render, templates
from app.folders import service as folder_service
from app.hosts import charts as host_charts
from app.hosts import service as host_service

router = APIRouter(prefix="/hosts", tags=["hosts"],
                   dependencies=[Depends(require_csrf), Depends(require_permission("view"))])

TABS = {"summary": "Ringkasan", "apps": "Aplikasi", "network": "Jaringan", "history": "Riwayat",
        "screenshot": "Screenshot", "remote": "Remote", "actions": "Aksi"}


def _toast(message: str, **extra) -> dict:
    return {"HX-Trigger": json.dumps({"toast": message, **extra})}


def _cursor(after: str | None) -> tuple[str, int] | None:
    """Cursor keyset "hostname|id"."""
    if not after or "|" not in after:
        return None
    name, _, ident = after.rpartition("|")
    try:
        return name, int(ident)
    except ValueError:
        raise HTTPException(status_code=422) from None


# ---------- Komputer ----------

@router.get("")
async def list_page(
    request: Request, user: CurrentUserDep, db: DbSession,
    q: str = Query("", max_length=60), group: str = "folder", status: str = "",
):
    threshold = await host_service.offline_after(db)
    tree = await folder_service.load_tree(db)
    summaries = await host_service.group_summaries(db, threshold)
    q = q.strip()
    context = {"tree": tree, "summaries": summaries, "q": q, "group": group, "status": status,
               "total": sum(s.total for s in summaries.values())}
    if q or group in ("none", "status"):
        rows, cursor = await host_service.list_hosts(db, threshold, q=q or None, status=status or None)
        query = {k: v for k, v in {"q": q, "status": status}.items() if v}
        context.update(rows=rows, cursor=cursor, flat=True, base_query=urlencode(query))
    return render(request, "hosts/list.html", "hosts/list.html", context)


@router.get("/rows")
async def rows(
    request: Request, user: CurrentUserDep, db: DbSession,
    unit: int | None = None, unassigned: bool = False, problems: bool = False,
    q: str = Query("", max_length=60), status: str = "", after: str | None = None,
):
    """Isi satu grup (lazy load saat grup dibuka) atau halaman berikutnya ("Muat lagi")."""
    threshold = await host_service.offline_after(db)
    tree = await folder_service.load_tree(db)
    folder_ids = None
    if unit is not None:
        if unit not in tree.by_id:
            raise HTTPException(status_code=404)
        folder_ids = tree.subtree_ids(unit)
    rows_, cursor = await host_service.list_hosts(
        db, threshold, folder_ids=folder_ids, unassigned=unassigned, q=q.strip() or None,
        status=status or None, problems_only=problems, after=_cursor(after))
    query = {"unit": unit, "unassigned": int(unassigned) or None, "problems": int(problems) or None,
             "q": q.strip() or None, "status": status or None}
    context = {"rows": rows_, "cursor": cursor, "tree": tree, "unit": unit, "unassigned": unassigned,
               "problems": problems, "q": q, "status": status, "summary": None,
               "base_query": urlencode({k: v for k, v in query.items() if v is not None}),
               "suggest": folder_service.suggest}
    if unit is not None or unassigned:
        context["summary"] = (await host_service.group_summaries(db, threshold)).get(None if unassigned else unit)
    name = "hosts/_rows.html" if after else "hosts/_group.html"
    return templates.TemplateResponse(request=request, name=name, context=context)


@router.post("/move", dependencies=[Depends(require_permission("pc_manage"))])
async def move(request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    try:
        ids = [int(v) for v in form.getlist("host_id")]
        folder_id = int(form.get("folder_id") or 0) or None
    except ValueError as err:
        raise HTTPException(status_code=400) from err
    if not ids:
        raise HTTPException(status_code=422, detail="Pilih minimal satu PC.")
    async with db.begin():                 # bacaan selesai sebelum service membuka transaksinya
        tree = await folder_service.load_tree(db)
    try:
        moved = await host_service.move_hosts(db, ids, folder_id, tree, user.actor)
    except host_service.HostActionError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return Response(status_code=204, headers={"HX-Refresh": "true", **_toast(f"{moved} PC dipindahkan.")})


# ---------- Folder ----------

@router.get("/folders", dependencies=[Depends(require_permission("folders_manage"))])
async def folders_page(request: Request, user: CurrentUserDep, db: DbSession):
    threshold = await host_service.offline_after(db)
    tree = await folder_service.load_tree(db)
    counts = await host_service.folder_counts(db)
    return render(request, "hosts/folders.html", "hosts/folders.html",
                  {"tree": tree, "counts": counts, "summaries": await host_service.group_summaries(db, threshold)})


def _folder_form(form) -> dict:
    return {k: (str(form.get(k, "")).strip() or None) for k in ("name", "location", "subnet_hint")}


@router.post("/folders", dependencies=[Depends(require_permission("folders_manage"))])
async def create_folder(request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    data = _folder_form(form)
    try:
        parent_id = int(form.get("parent_id") or 0) or None
        await folder_service.create_folder(db, name=data["name"] or "", parent_id=parent_id,
                                           location=data["location"], subnet_hint=data["subnet_hint"],
                                           actor=user.actor)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/folders/{folder_id}", dependencies=[Depends(require_permission("folders_manage"))])
async def update_folder(folder_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    data = _folder_form(await request.form())
    try:
        await folder_service.update_folder(db, folder_id, name=data["name"] or "", location=data["location"],
                                           subnet_hint=data["subnet_hint"], actor=user.actor)
    except folder_service.FolderError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/folders/{folder_id}/delete", dependencies=[Depends(require_permission("folders_manage"))])
async def delete_folder(folder_id: int, user: CurrentUserDep, db: DbSession):
    async with db.begin():
        count = (await host_service.folder_counts(db)).get(folder_id, 0)
    try:
        await folder_service.delete_folder(db, folder_id, host_count=count, actor=user.actor)
    except folder_service.FolderError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return Response(status_code=204, headers={"HX-Refresh": "true"})


# ---------- Detail Komputer ----------

async def _detail(db, host_id: int):
    threshold = await host_service.offline_after(db)
    detail = await host_service.get_detail(db, host_id, threshold)
    if detail is None or detail.host.status not in host_service.APPROVED:
        raise HTTPException(status_code=404)
    return detail, threshold


@router.get("/{host_id}")
async def detail_page(
    request: Request, host_id: int, user: CurrentUserDep, db: DbSession,
    tab: str = "summary", range: str = "24h",
):
    if tab not in TABS:
        tab = "summary"
    detail, threshold = await _detail(db, host_id)
    tree = await folder_service.load_tree(db)
    context = {"d": detail, "h": detail.host, "tab": tab, "tabs": TABS, "tree": tree}
    context.update(await _tab_context(db, detail, tab, range, q=""))
    if tab == "summary" or not request.headers.get("HX-Request"):
        prev, nxt, position, total = await host_service.neighbors(db, detail.host)
        context.update(prev=prev, next=nxt, position=position, folder_total=total)
    return render(request, "hosts/detail.html", "hosts/_tabs.html", context)


async def _tab_context(db, detail, tab: str, range_key: str, q: str) -> dict:
    host_id = detail.host.id
    if tab == "summary":
        events, _ = await host_service.events(db, host_id, limit=5)
        return {"charts": await host_charts.host_charts(db, host_id, range_key),
                "ranges": host_charts.RANGE_LABELS, "trends": await host_charts.disk_trends(db, host_id),
                "recent_events": events}
    if tab == "apps":
        return {"apps": await host_service.apps(db, host_id, q=q or None), "q": q, "showing_all": False}
    if tab == "network":
        return {"adapters": await host_service.adapters(db, host_id),
                "charts": await host_charts.host_charts(db, host_id, "24h")}
    if tab == "history":
        events, cursor = await host_service.events(db, host_id)
        return {"events": events, "cursor": cursor}
    if tab == "screenshot":
        return {"shots": await host_service.screenshots(db, host_id)}
    if tab == "actions":
        return {}
    return {"sessions": await host_service.remote_sessions(db, host_id), "end_reasons": host_service.REMOTE_END_REASONS}


@router.get("/{host_id}/charts")
async def charts(request: Request, host_id: int, user: CurrentUserDep, db: DbSession, range: str = "24h"):
    detail, _ = await _detail(db, host_id)
    return templates.TemplateResponse(request=request, name="hosts/_charts.html", context={
        "h": detail.host, "d": detail, "charts": await host_charts.host_charts(db, host_id, range),
        "ranges": host_charts.RANGE_LABELS})


@router.get("/{host_id}/apps")
async def apps(request: Request, host_id: int, user: CurrentUserDep, db: DbSession,
               q: str = Query("", max_length=100), all: bool = False):
    detail, _ = await _detail(db, host_id)
    rows_ = await host_service.apps(db, host_id, q=q.strip() or None, limit=None if all or q else 20)
    return templates.TemplateResponse(request=request, name="hosts/_app_rows.html", context={
        "h": detail.host, "d": detail, "apps": rows_, "q": q, "showing_all": all or bool(q)})


@router.get("/{host_id}/events")
async def more_events(request: Request, host_id: int, user: CurrentUserDep, db: DbSession, before: int):
    await _detail(db, host_id)             # 404 untuk PC pending/ditolak, sama seperti halaman detail
    events, cursor = await host_service.events(db, host_id, before_id=before)
    return templates.TemplateResponse(request=request, name="hosts/_events.html", context={
        "events": events, "cursor": cursor, "h": {"id": host_id}, "continued": True})


@router.post("/{host_id}/inventory", dependencies=[Depends(require_permission("pc_manage"))])
async def refresh_inventory(host_id: int, user: CurrentUserDep, db: DbSession):
    try:
        await host_service.request_inventory(db, host_id, user.actor)
    except host_service.HostActionError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    return Response(status_code=204, headers=_toast("Permintaan refresh inventaris dikirim pada heartbeat berikutnya."))


@router.post("/{host_id}/specs", dependencies=[Depends(require_permission("pc_manage"))])
async def update_specs(host_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    values = {k: str(form.get(k, "")) for k in host_service.EDITABLE_SPECS}
    try:
        changed = await host_service.update_specs(db, host_id, values, user.actor)
    except host_service.HostActionError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if not changed:
        return Response(status_code=204, headers=_toast("Tidak ada perubahan.", **{"close-dialog": True}))
    return Response(status_code=204, headers={"HX-Refresh": "true", **_toast("Data PC disimpan.")})


@router.post("/{host_id}/revoke", dependencies=[Depends(require_permission("agent_enroll"))])
async def revoke(host_id: int, user: CurrentUserDep, db: DbSession):
    try:
        await host_service.revoke_token(db, host_id, user.actor)
    except host_service.HostActionError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    return Response(status_code=204, headers={"HX-Redirect": "/hosts"})


@router.post("/{host_id}/screenshots", dependencies=[Depends(require_permission("screenshot"))])
async def take_screenshot(request: Request, host_id: int, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    async with db.begin():
        detail, threshold = await _detail(db, host_id)
    error = None
    try:
        await host_service.request_screenshot(
            db, host_id, reason=str(form.get("reason", "")).strip(), notify_user=form.get("notify_user") == "1",
            actor=user.actor, threshold=threshold)
    except host_service.HostActionError as err:
        error = str(err)
    return templates.TemplateResponse(
        request=request, name="hosts/_tab_screenshot.html", status_code=422 if error else 200,
        context={"d": detail, "h": detail.host, "shots": await host_service.screenshots(db, host_id),
                 "error": error, "reason": form.get("reason", "") if error else ""})


@router.get("/{host_id}/screenshots/{shot_id}", dependencies=[Depends(require_permission("screenshot"))])
async def screenshot_image(host_id: int, shot_id: int, user: CurrentUserDep, db: DbSession):
    async with db.begin():
        detail, _ = await _detail(db, host_id)
        shot = await host_service.get_screenshot(db, host_id, shot_id)
    if shot is None or shot.status != "uploaded" or not shot.file_path:
        raise HTTPException(status_code=404)
    root = settings.SCREENSHOT_DIR.resolve()
    path = (root / shot.file_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404)
    await host_service.record_screenshot_view(db, detail.host, shot, user.actor)
    # Data pasien mungkin terlihat: jangan disimpan cache browser/proxy (default middleware no-store)
    return FileResponse(path, headers={"Content-Disposition": "inline"})
