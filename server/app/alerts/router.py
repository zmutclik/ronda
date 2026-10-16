import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.alerts import rules as rule_text
from app.alerts import service as alert_service
from app.alerts.models import LEVELS, METRICS, OPERATORS
from app.auth.dependencies import CurrentUserDep, require_csrf, require_permission
from app.core.database import DbSession
from app.core.dependencies import get_http_client
from app.core.templating import localtime, render, templates
from app.folders.service import load_tree
from app.integrations.telegram import SendError

router = APIRouter(prefix="/alerts", tags=["alerts"],
                   dependencies=[Depends(require_csrf), Depends(require_permission("view"))])

TABS = {"active": "Aktif", "history": "Riwayat", "rules": "Aturan & Notifikasi"}


def _filters(request: Request) -> alert_service.AlertFilters:
    p = request.query_params

    def as_int(key: str) -> int | None:
        try:
            return int(p.get(key) or 0) or None
        except ValueError:
            raise HTTPException(status_code=422) from None

    return alert_service.AlertFilters(
        level=p.get("level") or None, unit_id=as_int("unit"), rule_id=as_int("rule"),
        unacked=p.get("unacked") == "1", range_key=p.get("range") if p.get("range") in alert_service.HISTORY_RANGES
        else "7d",
    )


async def _panel_context(db, tab: str, f: alert_service.AlertFilters) -> dict:
    context = {"tab": tab, "tabs": TABS, "f": f, "kpis": await alert_service.kpis(db), "tree": await load_tree(db),
               "rule_options": await alert_service.rule_options(db), "levels": LEVELS,
               "ranges": alert_service.HISTORY_RANGES}
    if tab == "active":
        context["alerts"] = await alert_service.active_alerts(db, f)
    elif tab == "history":
        rows, cursor, total = await alert_service.history(db, f)
        context.update(alerts=rows, cursor=cursor, total=total)
    else:
        context.update(rules=await alert_service.rules(db), channels=await alert_service.channels(db),
                       quiet=await alert_service.quiet_hours(db), metrics=METRICS, operators=OPERATORS,
                       describe=rule_text.describe)
    return context


@router.get("")
async def alerts_page(request: Request, user: CurrentUserDep, db: DbSession, tab: str = "active"):
    tab = tab if tab in TABS else "active"
    context = await _panel_context(db, tab, _filters(request))
    return render(request, "alerts/index.html", "alerts/_panel.html", context)


@router.get("/history/more")
async def history_more(request: Request, user: CurrentUserDep, db: DbSession, before: int):
    f = _filters(request)
    rows, cursor, total = await alert_service.history(db, f, before_id=before)
    return templates.TemplateResponse(request=request, name="alerts/_history_rows.html", context={
        "alerts": rows, "cursor": cursor, "tree": await load_tree(db), "f": f})


@router.get("/export.csv")
async def export_csv(request: Request, user: CurrentUserDep, db: DbSession):
    f = _filters(request)
    tree = await load_tree(db)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Level", "PC", "Folder", "Aturan", "Pesan", "Mulai", "Selesai", "Diselesaikan"])
    for a in await alert_service.history_export(db, f):
        writer.writerow([LEVELS[a.alert.level], a.host_name, tree.path(a.folder_id), a.rule_text, a.alert.message,
                         localtime(a.alert.started_at), localtime(a.alert.resolved_at), a.alert.resolve_note or ""])
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="alert-{f.range_key}.csv"'})


async def _ids(request: Request) -> tuple[list[int], dict]:
    form = await request.form()
    try:
        ids = [int(v) for v in form.getlist("alert_id")]
    except ValueError as err:
        raise HTTPException(status_code=400) from err
    if not ids:
        raise HTTPException(status_code=422, detail="Pilih minimal satu alert.")
    return ids, form


async def _after_action(request: Request, db, message: str):
    context = await _panel_context(db, "active", _filters(request))
    return templates.TemplateResponse(request=request, name="alerts/_panel.html", context=context,
                                      headers={"HX-Trigger": json.dumps({"toast": message})})


@router.post("/ack", dependencies=[Depends(require_permission("alert_handle"))])
async def acknowledge(request: Request, user: CurrentUserDep, db: DbSession):
    ids, form = await _ids(request)
    try:
        n = await alert_service.acknowledge(db, ids, user.actor, str(form.get("note", "")).strip() or None)
    except alert_service.AlertError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _after_action(request, db, f"{n} alert diakui.")


@router.post("/mute", dependencies=[Depends(require_permission("alert_handle"))])
async def mute(request: Request, user: CurrentUserDep, db: DbSession):
    ids, _ = await _ids(request)
    n = await alert_service.mute(db, ids, user.actor)
    return await _after_action(request, db, f"{n} alert dibisukan {alert_service.MUTE_HOURS} jam.")


@router.post("/resolve", dependencies=[Depends(require_permission("alert_handle"))])
async def resolve(request: Request, user: CurrentUserDep, db: DbSession):
    ids, form = await _ids(request)
    n = await alert_service.resolve(db, ids, user.actor, str(form.get("note", "")))
    return await _after_action(request, db, f"{n} alert diselesaikan.")


# ---------- Aturan & kanal (Admin) ----------

# Disertakan ke `router` (prefix /alerts, CSRF, view) di bawah; di sini hanya tambahan hak akses Admin
rules_router = APIRouter(dependencies=[Depends(require_permission("alert_rules"))])


async def _rules_panel(request: Request, db, message: str):
    context = await _panel_context(db, "rules", alert_service.AlertFilters())
    return templates.TemplateResponse(request=request, name="alerts/_panel.html", context=context,
                                      headers={"HX-Trigger": json.dumps({"toast": message, "close-dialog": True})})


@rules_router.post("/rules")
@rules_router.post("/rules/{rule_id}")
async def save_rule(request: Request, user: CurrentUserDep, db: DbSession, rule_id: int | None = None):
    try:
        data = alert_service.parse_rule(await request.form())
        rule = await alert_service.save_rule(db, rule_id, data, user.actor)
    except alert_service.AlertError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _rules_panel(request, db, f"Aturan “{rule.name}” disimpan.")


@rules_router.post("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    try:
        await alert_service.toggle_rule(db, rule_id, form.get("active") == "1", user.actor)
    except alert_service.AlertError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _rules_panel(request, db, "Status aturan diperbarui.")


@rules_router.post("/channels")
async def add_channel(request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    try:
        await alert_service.add_channel(db, str(form.get("kind", "")), str(form.get("name", "")).strip(),
                                        str(form.get("target", "")).strip(), user.actor)
    except alert_service.AlertError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _rules_panel(request, db, "Kanal ditambahkan.")


@rules_router.post("/channels/{channel_id}/delete")
async def remove_channel(channel_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    try:
        await alert_service.remove_channel(db, channel_id, user.actor)
    except alert_service.AlertError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _rules_panel(request, db, "Kanal dihapus.")


@rules_router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    channel = await alert_service.get_channel(db, channel_id)
    if channel is None:
        raise HTTPException(status_code=404)
    # I/O pihak ketiga langsung di request (bukan job): admin butuh hasilnya sekarang; timeout adapter 5 detik
    try:
        await alert_service.send_to_channel(get_http_client(request), channel,
                                            f"Tes notifikasi Ronda dari {user.username}.")
    except Exception as err:                 # SendError, juga error jaringan httpx: dilaporkan ke admin
        detail = str(err) if isinstance(err, SendError) else "Gagal menghubungi layanan notifikasi."
        return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": f"Gagal: {detail}"})})
    return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": "Pesan tes terkirim."})})


@rules_router.post("/quiet-hours")
async def quiet_hours(request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    try:
        await alert_service.save_quiet_hours(db, str(form.get("start", "")), str(form.get("end", "")),
                                             form.get("critical_still_sent") == "1", user.actor)
    except alert_service.AlertError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _rules_panel(request, db, "Jam tenang disimpan.")


router.include_router(rules_router)
