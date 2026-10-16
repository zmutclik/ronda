"""Aplikasi terpasang dari registry Uninstall (seperti Apps & features) + hash untuk kirim-bila-berubah."""
import hashlib
import json
import logging
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import date

from ronda_agent import schemas

log = logging.getLogger(__name__)

NAME_MAX, VERSION_MAX, PUBLISHER_MAX = 255, 60, 150
APPS_MAX = 2000
UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_WOW = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
_SID_RE = re.compile(r"^S-1-5-21-[\d-]+$")       # akun user (bukan _Classes, bukan akun layanan)


def _install_date(raw) -> str | None:
    """Registry `YYYYMMDD` → `YYYY-MM-DD`; selain itu None."""
    text = str(raw or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:])).isoformat()
    except ValueError:
        return None


def normalize(entries: Iterable[dict]) -> list[dict]:
    """Nilai registry mentah per subkey → daftar aplikasi untuk server, urut per (nama, versi), tanpa duplikat.
    Lewati entri tanpa DisplayName, komponen sistem, dan update/patch (punya ParentKeyName)."""
    apps: dict[tuple[str, str], dict] = {}
    for e in entries:
        name = schemas.text(e.get("DisplayName"), NAME_MAX)
        if not name or str(e.get("SystemComponent", "")) == "1" or e.get("ParentKeyName"):
            continue
        version = schemas.text(e.get("DisplayVersion"), VERSION_MAX) or ""
        app = {"name": name, "version": version, "publisher": schemas.text(e.get("Publisher"), PUBLISHER_MAX),
               "install_date": _install_date(e.get("InstallDate"))}
        known = apps.get((name, version))
        if known is None:
            apps[(name, version)] = app
        else:   # duplikat (HKLM + HKU): gabung tanpa bergantung urutan baca registry, agar hash stabil
            for field in ("publisher", "install_date"):
                values = [v for v in (known[field], app[field]) if v is not None]
                known[field] = min(values) if values else None
    return [apps[k] for k in sorted(apps)][:APPS_MAX]


def apps_hash(apps: list[dict]) -> str:
    """SHA-256 dari JSON daftar terurut, sort_keys, tanpa spasi (dokumen client, "Aturan pengumpulan")."""
    return hashlib.sha256(json.dumps(apps, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------- Windows ----------

def _read_uninstall(winreg, root, path: str) -> Iterator[dict]:
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except OSError:
        return
    with key:
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                    values = {}
                    for j in range(winreg.QueryInfoKey(sub)[1]):
                        name, value, _ = winreg.EnumValue(sub, j)
                        values[name] = value
                    yield values
            except OSError:
                continue


def read_registry() -> Iterator[dict]:
    """HKLM (64-bit + WOW6432Node) dan HKU\\<SID> tiap user yang profilnya sedang dimuat."""
    if sys.platform != "win32":
        return
    import winreg
    yield from _read_uninstall(winreg, winreg.HKEY_LOCAL_MACHINE, UNINSTALL)
    yield from _read_uninstall(winreg, winreg.HKEY_LOCAL_MACHINE, UNINSTALL_WOW)
    with winreg.OpenKey(winreg.HKEY_USERS, "") as users:
        sids = [winreg.EnumKey(users, i) for i in range(winreg.QueryInfoKey(users)[0])]
    for sid in sids:
        if _SID_RE.match(sid):
            yield from _read_uninstall(winreg, winreg.HKEY_USERS, rf"{sid}\{UNINSTALL}")


def collect() -> list[dict]:
    return normalize(read_registry())
