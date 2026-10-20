"""Pengawas ronda-helper: menjaga satu helper berjalan di sesi user konsol, dari folder versi `current`.

Launcher (SYSTEM) yang menjalankan helper, bukan Task Scheduler: dengan begitu helper ikut pindah versi saat update/
rollback, dihidupkan ulang bila crash, dan token pipe diberikan lewat environment (tidak tampil di command line).
Helper dijalankan kecuali `screenshot = false` dan `remote = false` di config.toml (keduanya aktif bawaan; installer
/SCREENSHOT=0 /REMOTE=0 mematikan). Agent tetap menolak screenshot bila `screenshot = false`. Logika di sini tidak bergantung Windows;
winsession.py menyediakan `sessions` dan `spawn` untuk Windows.
"""
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ronda_agent.config import ConfigError, load_config
from ronda_agent.pipe import TOKEN_ENV, ensure_token
from ronda_agent.release import Layout

log = logging.getLogger("ronda_launcher.helper")

RESTART_DELAYS = (5, 15, 60, 300)
STABLE_RUN_SEC = 300


class Proc(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...


Spawn = Callable[[int, list[str], Path, dict[str, str]], Proc]   # (sesi, cmd, cwd, env tambahan) → proses


@dataclass
class _Running:
    proc: Proc
    session: int
    version: str
    started: float


def helper_command(version_dir: Path, data_dir: Path, session: int) -> list[str]:
    exe = version_dir / "ronda-helper" / ("ronda-helper.exe" if sys.platform == "win32" else "ronda-helper")
    if sys.platform != "win32" and not exe.exists():       # development: jalankan dari source
        return [sys.executable, "-m", "ronda_helper", "--data-dir", str(data_dir), "--session", str(session)]
    return [str(exe)]


class HelperSupervisor:
    def __init__(self, layout: Layout, sessions: Callable[[], int | None], spawn: Spawn, *,
                 enabled: Callable[[], bool] | None = None, command_for=helper_command,
                 check_sec: float = 5.0, restart_delays=RESTART_DELAYS, now: Callable[[], float] = time.monotonic):
        self.layout, self.sessions, self.spawn, self.command_for = layout, sessions, spawn, command_for
        self.enabled = enabled or self._enabled_in_config
        self.check_sec, self.restart_delays, self.now = check_sec, restart_delays, now
        self.running: _Running | None = None
        self.failures = 0
        self.next_check = 0.0
        self.not_before = 0.0
        self.last_wanted: tuple[int, str] | None = None

    def _enabled_in_config(self) -> bool:
        try:
            cfg = load_config(self.layout.data_dir / "config.toml")
        except ConfigError:
            return False
        return cfg.screenshot or cfg.remote       # remote: pemberitahuan sesi di layar user

    def tick(self, version: str | None) -> None:
        now = self.now()
        if now < self.next_check:
            return
        self.next_check = now + self.check_sec
        session = self.sessions() if version and self.enabled() else None
        wanted = (session, version) if session is not None and version else None
        if wanted != self.last_wanted:            # user/versi berganti: mulai baru tanpa sisa jeda crash
            self.failures, self.not_before, self.last_wanted = 0, 0.0, wanted

        r = self.running
        if r and (wanted is None or (r.session, r.version) != wanted):
            log.info("hentikan helper (sesi %s, versi %s)", r.session, r.version)
            self._kill()
            r = None
        if r and (code := r.proc.poll()) is not None:
            if now - r.started >= STABLE_RUN_SEC:
                self.failures = 0
            self.running = r = None
            delay = self.restart_delays[min(self.failures, len(self.restart_delays) - 1)]
            self.failures += 1
            self.not_before = now + delay
            log.warning("helper keluar dengan kode %s; jalankan lagi dalam %s dtk", code, delay)
        if r is None and wanted and now >= self.not_before:
            self._start(*wanted)

    def _start(self, session: int, version: str) -> None:
        version_dir = self.layout.version_dir(version)
        cmd = self.command_for(version_dir, self.layout.data_dir, session)
        try:
            token = ensure_token(self.layout.data_dir)
            proc = self.spawn(session, cmd, version_dir, {TOKEN_ENV: token})
        except Exception as err:          # sesi baru saja logoff, exe tidak ada, dsb.
            delay = self.restart_delays[min(self.failures, len(self.restart_delays) - 1)]
            self.failures += 1
            self.not_before = self.now() + delay
            log.error("helper tidak bisa dijalankan di sesi %s: %s; coba lagi %s dtk", session, err, delay)
            return
        log.info("helper %s berjalan di sesi %s", version, session)
        self.running = _Running(proc, session, version, self.now())

    def _kill(self) -> None:
        if self.running:
            try:
                self.running.proc.terminate()
            except Exception as err:
                log.warning("helper tidak bisa dihentikan: %s", err)
            self.running = None

    def stop(self) -> None:
        self._kill()


# ---------- di luar Windows (development) ----------

class _Popen:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def poll(self) -> int | None:
        return self.proc.poll()

    def terminate(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def spawn_local(session: int, cmd: list[str], cwd: Path, env: dict[str, str]) -> Proc:
    return _Popen(subprocess.Popen(cmd, cwd=cwd, env=os.environ | env, stdin=subprocess.DEVNULL))


def make_supervisor(layout: Layout) -> HelperSupervisor:
    if sys.platform == "win32":
        from ronda_launcher import winsession
        return HelperSupervisor(layout, winsession.active_console_session, winsession.spawn_in_session)
    return HelperSupervisor(layout, lambda: 0, spawn_local)
