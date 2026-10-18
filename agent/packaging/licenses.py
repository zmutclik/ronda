"""Kumpulkan teks lisensi semua komponen yang ikut di-bundle PyInstaller ke satu file (dijalankan build.ps1).

    python packaging/licenses.py --out dist/THIRD_PARTY_LICENSES.txt [--pyinstaller]

Isi: lisensi Ronda, Python (interpreter + pustaka bawaan yang ikut di-bundle), lalu tiap paket dependency runtime
`ronda-agent` (ditelusuri dari metadata paket terpasang, dengan marker platform; grup dev/build tidak ikut).
`--pyinstaller` menambah lisensi PyInstaller (bootloader ada di tiap .exe); wajib di build Windows.
Paket tanpa file lisensi → gagal, agar build tidak diam-diam membawa komponen tanpa teks lisensinya.
"""
import argparse
import importlib.metadata as md
import os
import sys
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement   # ikut terpasang lewat pytest (dev) dan pyinstaller (build)

ROOT = Path(__file__).resolve().parents[1]
LICENSE_PREFIXES = ("LICEN", "COPYING", "NOTICE")


class LicenseError(Exception):
    pass


def _key(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def runtime_distributions(root: str = "ronda-agent") -> list[md.Distribution]:
    """`root` + semua dependency runtime-nya (tanpa extras), urut nama."""
    found: dict[str, md.Distribution] = {}
    todo = [root]
    while todo:
        name = todo.pop()
        if _key(name) in found:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError as err:
            raise LicenseError(f"paket {name} tidak terpasang (jalankan uv sync dulu)") from err
        found[_key(name)] = dist
        for raw in dist.requires or ():
            req = Requirement(raw)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return [found[k] for k in sorted(found)]


def license_files(dist: md.Distribution) -> list[PurePosixPath]:
    """File lisensi di .dist-info (PEP 639 `licenses/` atau nama LICENSE*/COPYING*/NOTICE*); bila tidak ada, file
    bernama serupa di mana saja dalam paket (mis. pywin32 `win32/license.txt`)."""
    files = [PurePosixPath(str(f)) for f in dist.files or ()]

    def is_license(f: PurePosixPath) -> bool:
        return f.name.upper().startswith(LICENSE_PREFIXES)

    in_meta = [f for f in files if f.parts and f.parts[0].endswith(".dist-info")
               and ("licenses" in f.parts[1:-1] or is_license(f))]
    return sorted(in_meta or [f for f in files if is_license(f)])


def python_license() -> Path:
    for candidate in (Path(sys.base_prefix) / "LICENSE.txt", Path(os.__file__).parent / "LICENSE.txt"):
        if candidate.is_file():
            return candidate
    raise LicenseError("LICENSE.txt Python tidak ditemukan")


def _section(title: str, body: str) -> str:
    return f"{'=' * 100}\n{title}\n{'=' * 100}\n\n{body.strip()}\n\n"


def _license_name(dist: md.Distribution) -> str:
    expr = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
    return expr if expr and "\n" not in expr and len(expr) <= 60 else ""


def render(*, pyinstaller: bool) -> tuple[str, int]:
    """(teks, jumlah komponen)."""
    dists = [d for d in runtime_distributions() if _key(d.metadata["Name"]) != "ronda-agent"]
    if pyinstaller:
        try:
            dists.append(md.distribution("pyinstaller"))
        except md.PackageNotFoundError as err:
            raise LicenseError("pyinstaller tidak terpasang (uv sync --group build)") from err
    parts = [
        "Ronda Agent — lisensi komponen\n\n"
        "Program ini dibangun dengan PyInstaller dan membawa interpreter Python serta paket di bawah ini.\n"
        "Tiap komponen tetap memakai lisensinya masing-masing; teks lengkapnya disertakan di file ini.\n\n",
        _section("Ronda Agent", (ROOT.parent / "LICENSE").read_text(encoding="utf-8")),
        _section(f"Python {sys.version.split()[0]}", python_license().read_text(encoding="utf-8", errors="replace")),
    ]
    missing = []
    for dist in dists:
        name, version = dist.metadata["Name"], dist.version
        files = license_files(dist)
        if not files:
            missing.append(name)
            continue
        expr = _license_name(dist)
        body = "\n\n".join(f"--- {f.name} ---\n\n{dist.locate_file(f).read_text(encoding='utf-8', errors='replace')}"
                           for f in files)
        parts.append(_section(f"{name} {version}" + (f" ({expr})" if expr else ""), body))
    if missing:
        raise LicenseError(f"file lisensi tidak ditemukan untuk: {', '.join(missing)}")
    return "".join(parts), len(parts) - 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--pyinstaller", action="store_true", help="sertakan lisensi PyInstaller (build Windows)")
    args = p.parse_args()
    try:
        text, count = render(pyinstaller=args.pyinstaller)
    except LicenseError as err:
        raise SystemExit(f"licenses: {err}") from err
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\r\n")    # dibuka di Notepad
    print(f"{args.out}: {count} komponen")


if __name__ == "__main__":
    main()
