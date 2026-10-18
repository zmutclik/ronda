"""Auto-update sisi agent: unduh → verifikasi → ekstrak ke versions\\<versi>\\ → tulis pending_switch.
Launcher yang memindah versi aktif (dan mengembalikannya bila versi baru gagal).

Unduh + verifikasi berjalan di thread sendiri agar heartbeat tetap jalan; loop utama memantau `result`.
"""
import logging
import shutil
import threading
from pathlib import Path

from ronda_agent import __version__
from ronda_agent.api import Api, ApiError
from ronda_agent.release import (MAX_PACKAGE_BYTES, Layout, ReleaseError, check_manifest, extract_package,
                                 verify_package, version_key)

log = logging.getLogger(__name__)


class UpdateJob:
    """Satu percobaan update di thread latar. `result`: None (jalan) | "ready" | "failed"."""

    def __init__(self, api: Api, layout: Layout, offer: dict, public_keys: tuple[str, ...] | None = None):
        self.api, self.layout, self.offer, self.public_keys = api, layout, offer, public_keys
        self.version = str(offer.get("version", ""))
        self.result: str | None = None
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, name=f"update-{self.version}", daemon=True)

    def start(self) -> "UpdateJob":
        self.thread.start()
        return self

    def _run(self) -> None:
        package: Path | None = None
        try:
            if version_key(self.version) <= version_key(__version__):
                raise ReleaseError(f"versi yang ditawarkan tidak lebih baru dari {__version__}")
            self.layout.incoming.mkdir(parents=True, exist_ok=True)
            package = self.layout.incoming / f"ronda-agent-{self.version}.zip"
            size = int(self.offer["size"])
            if not 0 < size <= MAX_PACKAGE_BYTES:
                raise ReleaseError(f"ukuran paket tidak wajar: {size}")
            log.info("mengunduh rilis %s (%d byte)", self.version, size)
            self.api.download(str(self.offer["url"]), package, max_bytes=size)
            verify_package(package, size=size, sha256=str(self.offer["sha256"]),
                           signature_b64=str(self.offer["signature"]), public_keys=self.public_keys)
            target = self.layout.version_dir(self.version)
            manifest = extract_package(package, target)
            try:
                check_manifest(manifest, expected_version=self.version,
                               launcher_version=self.layout.read_launcher_state().get("launcher_version"))
            except ReleaseError:
                shutil.rmtree(target, ignore_errors=True)
                raise
            self.layout.pending_switch.write_text(self.version, encoding="utf-8")
            log.info("rilis %s terverifikasi dan siap dipasang", self.version)
            self.result = "ready"
        except (ApiError, ReleaseError, OSError, KeyError, ValueError) as err:
            self.error = f"{type(err).__name__}: {err}"
            log.error("update ke %s gagal: %s", self.version, self.error)
            self.result = "failed"
        finally:
            if package is not None:
                package.unlink(missing_ok=True)
