"""Pemilih versi + pengawas proses agent: menjalankan versions\\<current>\\, memindah versi saat agent minta
(kode keluar 75 + file pending_switch), dan mengembalikan versi lama bila versi baru gagal.

Masa uji versi baru: dianggap gagal bila crash `crash_limit` kali dalam `crash_window_sec`, atau tidak menulis file
`healthy` dalam `trial_sec`. Logika ini tidak bergantung Windows; service.py hanya membungkusnya.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ronda_agent.release import UPDATE_EXIT_CODE, Layout, ReleaseError, version_key
from ronda_launcher import LAUNCHER_VERSION
from ronda_launcher.trust import OwnerOf, default_owner_of, untrusted_reason

log = logging.getLogger("ronda_launcher")

_DEFAULT = object()
RESTART_DELAYS = (5, 10, 30, 60)      # crash di luar masa uji: restart dengan jeda bertahap
STABLE_RUN_SEC = 300                  # agent jalan selama ini → jeda restart kembali ke awal


def default_command(version_dir: Path, data_dir: Path) -> list[str]:
    exe = version_dir / "ronda-agent" / ("ronda-agent.exe" if sys.platform == "win32" else "ronda-agent")
    return [str(exe), "--data-dir", str(data_dir)]


class Launcher:
    def __init__(self, data_dir: Path, command_for: Callable[[Path, Path], list[str]] = default_command, *,
                 trial_sec: float = 300, crash_window_sec: float = 300, crash_limit: int = 3,
                 stable_sec: float = 24 * 3600, poll_sec: float = 1.0, restart_delays=RESTART_DELAYS,
                 now: Callable[[], float] = time.time, on_switch: Callable[[str], None] | None = None,
                 helper=None, owner_of: OwnerOf | None = _DEFAULT):
        self.layout = Layout(data_dir)
        self.command_for = command_for
        self.trial_sec, self.crash_window_sec, self.crash_limit = trial_sec, crash_window_sec, crash_limit
        self.stable_sec, self.poll_sec, self.restart_delays = stable_sec, poll_sec, restart_delays
        self.now = now
        self.on_switch = on_switch or (lambda version: None)
        self.helper = helper            # HelperSupervisor: helper ikut versi `current` (pindah versi/rollback)
        self.state = self.layout.read_launcher_state()
        self.crashes: list[float] = []
        # Pemilik file versi dicek sebelum dijalankan (trust.py); None = tanpa cek (di luar Windows)
        self.owner_of = default_owner_of() if owner_of is _DEFAULT else owner_of
        self._rejected: set[str] = set()      # versi yang sudah dicatat ditolak: log sekali, bukan tiap putaran

    # ---------- state ----------

    @property
    def current(self) -> str | None:
        """Versi aktif, atau None bila belum ada atau isinya rusak (mis. listrik mati saat menulis): pemanggil lalu
        memakai _bootstrap(), bukan berhenti karena ReleaseError dari version_dir()."""
        try:
            version = self.layout.current_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, UnicodeDecodeError):
            return None
        try:
            version_key(version)
        except ReleaseError:
            if version:
                log.error("file current berisi versi tidak valid: %r", version[:40])
            return None
        return version

    def _trusted(self, version: str) -> bool:
        """Folder versi ada, dan (Windows) seluruh isinya dimiliki SYSTEM/Administrators tanpa symlink/junction."""
        version_dir = self.layout.version_dir(version)
        if not version_dir.is_dir():
            return False
        if self.owner_of is None:
            return True
        reason = untrusted_reason(self.layout.data_dir, version_dir, self.owner_of)
        if reason is None:
            self._rejected.discard(version)
            return True
        if version not in self._rejected:
            log.error("versi %s TIDAK dijalankan: %s", version, reason)
            self._rejected.add(version)
        return False

    def _set_current(self, version: str) -> None:
        _atomic_write(self.layout.current_file, version)

    def _save(self) -> None:
        self.state["launcher_version"] = LAUNCHER_VERSION
        _atomic_write(self.layout.launcher_state, json.dumps(self.state, indent=1))

    @property
    def trial(self) -> dict | None:
        return self.state.get("trial")

    # ---------- pindah versi / rollback ----------

    def _bootstrap(self) -> bool:
        """`current` belum ada (instalasi baru) atau tidak bisa dipakai: versi terpasang tertinggi yang lolos _trusted."""
        if not self.layout.versions.is_dir():
            return False
        found = []
        for d in self.layout.versions.iterdir():
            try:
                version_key(d.name)
            except ReleaseError:
                continue
            if self._trusted(d.name):
                found.append(d.name)
        if not found:
            return False
        version = max(found, key=version_key)
        log.info("memakai versi terpasang %s", version)
        self._set_current(version)
        return True

    def apply_pending_switch(self) -> None:
        marker = self.layout.pending_switch
        if not marker.exists():
            return
        new = marker.read_text(encoding="utf-8").strip()
        marker.unlink()
        try:
            version_key(new)
        except ReleaseError:
            log.error("pending_switch berisi versi tidak valid: %r", new)
            return
        old = self.current
        if new == old or not self._trusted(new):
            log.error("pending_switch %s diabaikan (sama dengan current, folder tidak ada, atau ditolak)", new)
            return
        log.info("pindah versi %s → %s (masa uji %.0f dtk)", old, new, self.trial_sec)
        self.layout.healthy.unlink(missing_ok=True)
        # cleaned=False: versi yang kini jadi lama (sebelum `previous`) dibersihkan lagi setelah versi baru stabil
        self.state.update(previous=old, trial={"version": new, "since": self.now()}, update_status="installed",
                          stable_since=None, cleaned=False)
        self._save()
        self._set_current(new)
        self.crashes.clear()
        self.on_switch(new)

    def rollback(self, reason: str) -> bool:
        trial, previous = self.trial, self.state.get("previous")
        if not trial or not previous or not self._trusted(previous):
            log.error("versi %s gagal (%s) tapi tidak ada versi sebelumnya untuk dipulihkan", self.current, reason)
            self.state["trial"] = None
            self._save()
            return False
        bad = trial["version"]
        log.error("versi %s gagal (%s); kembali ke %s", bad, reason, previous)
        bad_versions = [v for v in self.state.get("bad_versions") or [] if v != bad] + [bad]
        self.state.update(trial=None, previous=None, update_status="rollback", bad_versions=bad_versions[-10:],
                          stable_since=None, cleaned=False)
        self._save()
        self._set_current(previous)
        self.crashes.clear()
        self.on_switch(previous)
        return True

    def _end_trial_if_healthy(self) -> None:
        if self.trial and self.layout.healthy.exists():
            log.info("versi %s sehat; masa uji selesai", self.trial["version"])
            self.state.update(trial=None, stable_since=self.now())
            self._save()

    def cleanup_old_versions(self) -> None:
        """Setelah stabil `stable_sec`: hapus folder versi selain current dan previous."""
        since = self.state.get("stable_since")
        if self.trial or since is None or self.now() - since < self.stable_sec or self.state.get("cleaned"):
            return
        keep = {self.current, self.state.get("previous")}
        for d in self.layout.versions.iterdir() if self.layout.versions.is_dir() else ():
            if d.is_dir() and d.name not in keep:
                log.info("hapus versi lama %s", d.name)
                shutil.rmtree(d, ignore_errors=True)
        self.state["cleaned"] = True
        self._save()

    # ---------- proses agent ----------

    def _start(self, version: str) -> subprocess.Popen:
        cmd = self.command_for(self.layout.version_dir(version), self.layout.data_dir)
        log.info("menjalankan agent %s", version)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags, cwd=self.layout.version_dir(version))

    def _watch(self, proc: subprocess.Popen, stop: threading.Event) -> int | str | None:
        """Tunggu agent selesai. Kembalikan kode keluar, "trial_timeout", atau None bila diminta berhenti."""
        while True:
            code = proc.poll()
            if code is not None:
                self._end_trial_if_healthy()     # sempat sehat sebelum keluar (mis. keluar 75 untuk update)
                return code
            if stop.wait(self.poll_sec):
                _terminate(proc)
                return None
            self._end_trial_if_healthy()
            self._tick_helper()
            if self.trial and self.now() - self.trial["since"] > self.trial_sec:
                _terminate(proc)
                return "trial_timeout"
            self.cleanup_old_versions()

    def _tick_helper(self) -> None:
        if self.helper:
            try:
                self.helper.tick(self.current)
            except Exception:
                log.exception("pengawas helper gagal")

    def run(self, stop: threading.Event) -> None:
        log.info("ronda-launcher %s mulai (data %s)", LAUNCHER_VERSION, self.layout.data_dir)
        self._save()
        restarts = 0
        while not stop.is_set():
            self.apply_pending_switch()
            version = self.current
            if not version or not self._trusted(version):
                if not self._bootstrap():
                    log.error("tidak ada versi agent terpasang di %s; cek lagi 60 dtk", self.layout.versions)
                    stop.wait(60)
                continue
            try:
                proc = self._start(version)
            except OSError as err:
                log.error("agent %s tidak bisa dijalankan: %s", version, err)
                if not (self.trial and self.rollback(f"gagal start: {err}")):
                    stop.wait(60)
                continue
            started = self.now()
            result = self._watch(proc, stop)
            if result is None:
                break
            if result == "trial_timeout":
                self.rollback(f"tidak sehat dalam {self.trial_sec:.0f} dtk")
                continue
            if result == UPDATE_EXIT_CODE and self.layout.pending_switch.exists():
                restarts = 0
                continue
            log.warning("agent %s keluar dengan kode %s", version, result)
            if self.trial:
                now = self.now()
                self.crashes = [t for t in self.crashes if now - t <= self.crash_window_sec] + [now]
                if len(self.crashes) >= self.crash_limit:
                    self.rollback(f"crash {len(self.crashes)}× dalam {self.crash_window_sec:.0f} dtk")
                    continue
            if self.now() - started >= STABLE_RUN_SEC:
                restarts = 0
            delay = self.restart_delays[min(restarts, len(self.restart_delays) - 1)]
            restarts += 1
            stop.wait(delay)
        if self.helper:
            self.helper.stop()
        log.info("ronda-launcher berhenti")


def _terminate(proc: subprocess.Popen, timeout: float = 15) -> None:
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
