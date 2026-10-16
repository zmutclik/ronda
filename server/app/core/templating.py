import functools
import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.discovery import APP_DIR


def _common_context(request: Request) -> dict:
    # csrf_token & current_user diisi get_current_user (app/auth/dependencies.py); kosong di halaman publik
    return {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "current_user": getattr(request.state, "user", None),
        "org_name": settings.ORG_NAME,
    }


STATIC_DIR = APP_DIR / "static"


def _file_version(path: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        return "missing"                     # mis. vendor belum diunduh: jangan gagalkan render


_cached_file_version = functools.cache(_file_version)   # production: file static tidak berubah selama proses hidup


def static_url(path: str) -> str:
    """URL static dengan hash isi file: setelah deploy browser langsung mengambil versi baru, sehingga nginx
    boleh memberi cache panjang untuk URL ber-?v= (deploy/nginx.conf)."""
    version = _file_version(path) if settings.DEBUG else _cached_file_version(path)
    return f"/static/{path}?v={version}"


def localtime(value: datetime | None, fmt: str = "%d-%m-%Y %H:%M") -> str:
    """DATETIME di DB = UTC tanpa zona (engine memaksa time_zone +00:00) → zona tampilan DISPLAY_TIMEZONE."""
    if value is None:
        return ""
    return value.replace(tzinfo=UTC).astimezone(_display_tz).strftime(fmt)


_display_tz = ZoneInfo(settings.DISPLAY_TIMEZONE)

MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def _to_local(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC).astimezone(_display_tz)


def shortdt(value: datetime | None) -> str:
    """Hari ini → "11:38"; tahun ini → "13 Okt 15:10"; selain itu → "13 Okt 2025"."""
    if value is None:
        return "—"
    local, today = _to_local(value), datetime.now(_display_tz)
    if local.date() == today.date():
        return local.strftime("%H:%M")
    if local.year == today.year:
        return f"{local.day:02d} {MONTHS[local.month - 1]} {local:%H:%M}"
    return f"{local.day:02d} {MONTHS[local.month - 1]} {local.year}"


def shortdate(value) -> str:
    """date/datetime → "14 Okt 2026" (datetime UTC dikonversi ke zona tampilan dulu)."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = _to_local(value)
    return f"{value.day:02d} {MONTHS[value.month - 1]} {value.year}"


def daylabel(value: datetime | None) -> str:
    """Judul hari di linimasa: "Hari ini", "Kemarin", "12 Okt", "12 Okt 2025"."""
    if value is None:
        return ""
    local, today = _to_local(value).date(), datetime.now(_display_tz).date()
    if local == today:
        return "Hari ini"
    if (today - local).days == 1:
        return "Kemarin"
    return f"{local.day:02d} {MONTHS[local.month - 1]}" + (f" {local.year}" if local.year != today.year else "")


def hhmm(value: datetime | None) -> str:
    return _to_local(value).strftime("%H:%M") if value else "—"


def ago(value: datetime | None) -> str:
    """Selisih dari sekarang, ringkas: "12 dtk", "14 mnt", "3 j", "21 hari". Hanya untuk tampilan."""
    if value is None:
        return "—"
    return duration((datetime.now(UTC) - value.replace(tzinfo=UTC)).total_seconds())


def duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} dtk"
    if s < 3600:
        return f"{s // 60} mnt"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} j {m} m" if m else f"{h} j"
    return f"{s // 86400} hari"


def fmt_bytes(value: int | float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            text = f"{size:.{digits}f}" if unit != "B" else f"{size:.0f}"
            return f"{text.replace('.', ',')} {unit}"
        size /= 1024
    return ""


def num(value, digits: int = 0) -> str:
    """Angka format Indonesia: 1.234 / 12,5."""
    if value is None:
        return "—"
    text = f"{float(value):,.{digits}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")

templates = Jinja2Templates(
    directory=[APP_DIR / "templates", *sorted(APP_DIR.glob("*/templates"))],
    context_processors=[_common_context],
)
templates.env.auto_reload = settings.DEBUG   # production: tanpa stat() file tiap render
templates.env.globals["static_url"] = static_url
templates.env.filters["localtime"] = localtime
templates.env.filters["shortdt"] = shortdt
templates.env.filters["shortdate"] = shortdate
templates.env.filters["ago"] = ago
templates.env.filters["daylabel"] = daylabel
templates.env.filters["hhmm"] = hhmm
templates.env.filters["duration"] = duration
templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["num"] = num


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def is_htmx_partial(request: Request) -> bool:
    # History restore (tombol back setelah cache miss) butuh full page
    return is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"


def render(request: Request, page: str, fragment: str, context: dict, **kwargs):
    name = fragment if is_htmx_partial(request) else page
    response = templates.TemplateResponse(request=request, name=name, context=context, **kwargs)
    response.headers["Vary"] = "HX-Request"  # cegah browser/proxy menyajikan fragment sebagai full page
    return response
