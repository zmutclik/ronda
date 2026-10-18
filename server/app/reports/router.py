from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.auth.dependencies import CurrentUserDep, require_csrf, require_permission
from app.core.config import settings
from app.core.database import DbSession
from app.core.dates import parse_day
from app.core.templating import render, shortdate, templates
from app.folders.service import load_tree
from app.reports import service as report_service

router = APIRouter(prefix="/reports", tags=["reports"],
                   dependencies=[Depends(require_csrf), Depends(require_permission("report_export"))])

PREVIEW_ROWS = 20
MAX_RANGE_DAYS = 366        # setahun (retensi metrics_hourly); lebih panjang = query berat tanpa data
_tz = ZoneInfo(settings.DISPLAY_TIMEZONE)


def _to_utc(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz).astimezone(UTC).replace(tzinfo=None)


async def _resolve(request: Request, user, db):
    """Parameter laporan dari query string → (key, tree, Params, nilai form untuk dirender ulang)."""
    q = request.query_params
    key = q.get("report") if q.get("report") in report_service.REPORTS else "inventory"
    if key in report_service.SENSITIVE and not user.can("report_sensitive"):
        raise HTTPException(status_code=403)
    tree = await load_tree(db)
    try:
        folder = int(q.get("folder") or 0) or None
        today = datetime.now(_tz).date()
        end = parse_day(q.get("to"), today)
        start = parse_day(q.get("from"), end - timedelta(days=29))
    except ValueError:
        raise HTTPException(status_code=422) from None
    if folder is not None and folder not in tree.by_id:
        raise HTTPException(status_code=404)
    start, end = min(start, end), max(start, end)
    if key in report_service.RANGED and (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"Periode laporan maksimal {MAX_RANGE_DAYS} hari.")
    params = report_service.Params(
        folder_ids=tree.subtree_ids(folder) if folder else None,
        folder_label=tree.path(folder) if folder else "Semua folder",
    )
    if key in report_service.RANGED:
        params.start, params.end = _to_utc(start), _to_utc(end + timedelta(days=1))
        params.period_label = f"{shortdate(start)} – {shortdate(end)}"
    form = {"report": key, "folder": folder, "from": start.isoformat(), "to": end.isoformat()}
    return key, tree, params, form


def _printed() -> str:
    now = datetime.now(_tz)
    return f"{shortdate(now.date())} {now:%H:%M}"


@router.get("")
async def reports_page(request: Request, user: CurrentUserDep, db: DbSession):
    key, tree, params, form = await _resolve(request, user, db)
    report = await report_service.build(db, key, tree, params)
    context = {"reports": report_service.REPORTS, "sensitive": report_service.SENSITIVE,
               "ranged": report_service.RANGED, "tree": tree, "form": form, "report": report,
               "preview_rows": PREVIEW_ROWS, "printed": _printed(), "query": request.url.query}
    return render(request, "reports/index.html", "reports/_preview.html", context)


@router.get("/download")
async def download(request: Request, user: CurrentUserDep, db: DbSession):
    async with db.begin():                 # bacaan selesai sebelum record_download membuka transaksinya
        key, tree, params, _ = await _resolve(request, user, db)
        report = await report_service.build(db, key, tree, params)
    content = await run_in_threadpool(report_service.to_xlsx, report, settings.ORG_NAME, _printed())
    await report_service.record_download(db, key, "xlsx", params, user.actor)
    filename = f"ronda-{key}-{datetime.now(_tz):%Y%m%d}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/print")
async def print_view(request: Request, user: CurrentUserDep, db: DbSession):
    """Versi cetak (semua baris); "Simpan sebagai PDF" lewat dialog cetak browser."""
    async with db.begin():
        key, tree, params, form = await _resolve(request, user, db)
        report = await report_service.build(db, key, tree, params)
    await report_service.record_download(db, key, "pdf", params, user.actor)
    return templates.TemplateResponse(request=request, name="reports/print.html", context={
        "report": report, "printed": _printed(), "query": request.url.query})
