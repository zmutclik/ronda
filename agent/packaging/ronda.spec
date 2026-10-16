# PyInstaller (onedir), tiga program: dist/ronda-agent, dist/ronda-helper, dist/ronda-launcher.
# Dijalankan dari folder agent/ di Windows:  uv run pyinstaller --noconfirm --clean packaging/ronda.spec
# (biasanya lewat packaging/build.ps1)
from pathlib import Path

ROOT = Path(SPECPATH).parent
EXCLUDES = ["tkinter", "unittest", "pydoc", "test", "lib2to3"]


def program(entry, name, *, console, hidden=()):
    a = Analysis([str(ROOT / "packaging" / "entry" / entry)], pathex=[str(ROOT)], hiddenimports=list(hidden),
                 excludes=EXCLUDES)
    exe = EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, console=console, upx=False)
    return COLLECT(exe, a.binaries, a.datas, name=name, upx=False)


# agent: proses anak launcher (CREATE_NO_WINDOW); console=True supaya `--console` bisa dipakai saat diagnosis
program("agent.py", "ronda-agent", console=True, hidden=["win32timezone"])
# helper: berjalan di desktop user, tanpa jendela
program("helper.py", "ronda-helper", console=False)
# launcher: exe service (tanpa argumen = dijalankan Service Control Manager)
program("launcher.py", "ronda-launcher", console=True,
        hidden=["win32timezone", "win32serviceutil", "servicemanager"])
