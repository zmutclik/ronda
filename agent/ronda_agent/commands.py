"""Perintah screenshot: minta helper mengambil layar → unggah, atau lapor gagal ke server.

Berjalan di thread sendiri (helper bisa makan 15 dtk, unggah sampai 120 dtk) agar heartbeat tetap jalan; loop utama
memantau `result`.
"""
import logging
import threading
from collections.abc import Callable

from ronda_agent import schemas
from ronda_agent.api import Api, ApiError, Invalid, NotFound, TooLarge, Transient
from ronda_agent.pipe import HelperClient, HelperError, HelperUnavailable

log = logging.getLogger(__name__)

DISABLED_REASON = "Screenshot dinonaktifkan di PC ini"


class ScreenshotJob:
    """`result`: None (jalan) | "done" (selesai atau tidak perlu diulang) | "retry" (server tak terjangkau)."""

    def __init__(self, api: Api, helper: HelperClient | None, request_id: str,
                 logged_user: Callable[[], str | None]):
        self.api, self.helper, self.request_id, self.logged_user = api, helper, request_id, logged_user
        self.result: str | None = None
        self.thread = threading.Thread(target=self._run, name=f"screenshot-{request_id[:8]}", daemon=True)

    def start(self) -> "ScreenshotJob":
        self.thread.start()
        return self

    def _run(self) -> None:
        try:
            self.result = self._work()
        except Exception:
            log.exception("screenshot %s: galat tak terduga", self.request_id)
            self.result = "done"

    def _work(self) -> str:
        if self.helper is None:
            return self._fail(DISABLED_REASON)
        try:
            shot = self.helper.screenshot(self.request_id)
        except HelperUnavailable as err:
            return self._fail("Tidak ada user login" if self.logged_user() is None else str(err))
        except HelperError as err:
            return self._fail(str(err))
        try:
            self.api.screenshot_upload(self.request_id, shot.content, shot.content_type, shot.width, shot.height)
        except TooLarge:
            return self._fail("Gambar melebihi batas ukuran server")
        except (NotFound, Invalid) as err:
            log.info("screenshot %s tidak diterima (kedaluwarsa/selesai): %s", self.request_id, err)
            return "done"
        except Transient as err:
            log.warning("unggah screenshot %s gagal (%s); ulang di heartbeat berikutnya", self.request_id, err)
            return "retry"
        except ApiError as err:
            log.error("unggah screenshot %s ditolak: %s", self.request_id, err)
            return "done"
        log.info("screenshot %s terkirim (%dx%d, %d KB)", self.request_id, shot.width, shot.height,
                 len(shot.content) // 1024)
        return "done"

    def _fail(self, reason: str) -> str:
        reason = reason[:schemas.FAIL_REASON_MAX]
        try:
            self.api.screenshot_fail(self.request_id, reason)
        except (NotFound, Invalid):
            return "done"                # sudah kedaluwarsa/selesai
        except Transient:
            return "retry"
        except ApiError as err:
            log.error("lapor screenshot gagal ditolak: %s", err)
            return "done"
        log.info("screenshot %s dilaporkan gagal: %s", self.request_id, reason)
        return "done"
