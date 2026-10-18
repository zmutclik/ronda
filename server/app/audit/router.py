import csv
import io
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.audit import service as audit_service
from app.auth.dependencies import CurrentUserDep, require_csrf, require_permission
from app.auth.models import AUDIT_CATEGORIES
from app.core.config import settings
from app.core.database import DbSession
from app.core.dates import parse_day
from app.core.export import csv_safe
from app.core.templating import localtime, render, templates

router = APIRouter(prefix="/audit", tags=["audit"],
                   dependencies=[Depends(require_csrf), Depends(require_permission("audit_view"))])

_tz = ZoneInfo(settings.DISPLAY_TIMEZONE)


def _to_utc(local: datetime) -> datetime:
    return local.replace(tzinfo=_tz).astimezone(UTC).replace(tzinfo=None)


def _filters(request: Request) -> audit_service.AuditFilters:
    p = request.query_params
    f = audit_service.AuditFilters(
        range_key=p.get("range") if p.get("range") in audit_service.RANGES else "7d",
        username=(p.get("user") or None), category=p.get("category") if p.get("category") in AUDIT_CATEGORIES
        else None, target=(p.get("target") or "").strip()[:80] or None, sensitive=p.get("sensitive") == "1",
    )
    today = datetime.now(_tz).date()
    if f.range_key == "custom":
        try:
            start = parse_day(p.get("from"), today)
            end = parse_day(p.get("to"), today)
        except ValueError:
            raise HTTPException(status_code=422) from None
        f.start, f.end = audit_service.local_day_bounds(min(start, end), max(start, end), _to_utc)
    else:
        days = {"today": 0, "7d": 6, "30d": 29}[f.range_key]
        f.start, _ = audit_service.local_day_bounds(today - timedelta(days=days), today, _to_utc)
    return f


def _cursor(before: str | None) -> tuple[datetime, int] | None:
    if not before:
        return None
    try:
        ts, ident = before.rsplit("_", 1)
        return datetime.fromisoformat(ts), int(ident)
    except ValueError:
        raise HTTPException(status_code=422) from None


@router.get("")
async def audit_page(request: Request, user: CurrentUserDep, db: DbSession, before: str | None = None):
    f = _filters(request)
    rows, cursor = await audit_service.page(db, f, before=_cursor(before))
    query = request.url.query.split("&before=")[0] if request.url.query else ""
    context = {"rows": rows, "cursor": cursor, "f": f, "ranges": audit_service.RANGES,
               "categories": AUDIT_CATEGORIES, "usernames": await audit_service.usernames(db),
               "query": "&".join(part for part in query.split("&") if part and not part.startswith("before=")),
               "params": request.query_params}
    if before:
        return templates.TemplateResponse(request=request, name="audit/_rows.html", context=context)
    return render(request, "audit/index.html", "audit/_table.html", context)


@router.get("/export.csv")
async def export_csv(request: Request, user: CurrentUserDep, db: DbSession):
    f = _filters(request)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Waktu", "User", "Kategori", "Aksi", "Sensitif", "Target", "Detail", "IP"])
    for r in await audit_service.export(db, f):
        writer.writerow(map(csv_safe, [
            localtime(r.ts, "%Y-%m-%d %H:%M:%S"), r.username, AUDIT_CATEGORIES.get(r.category, r.category),
            r.action, "ya" if r.is_sensitive else "", r.target_label or "", r.detail or "", r.ip or ""]))
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'})
