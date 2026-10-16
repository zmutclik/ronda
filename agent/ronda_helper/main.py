"""ronda-helper: berjalan di sesi user (dijalankan launcher lewat CreateProcessAsUser), melayani agent lewat pipe.

Satu helper per sesi Windows. Token pipe diterima dari environment `RONDA_HELPER_TOKEN` (launcher), karena user biasa
tidak bisa membaca `local.token`. Log ke `%LOCALAPPDATA%\\Ronda\\logs\\helper.log`: folder log Ronda hanya bisa
ditulis SYSTEM; galat screenshot juga diteruskan ke agent dan tercatat di agent.log.

    ronda-helper                                         # Windows, dari launcher
    python -m ronda_helper --data-dir data --session 0   # development (token dibaca dari data/local.token)
"""
import argparse
import logging
import logging.handlers
import os
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import AuthenticationError, Connection, Listener
from pathlib import Path

from ronda_agent import __version__
from ronda_agent.pipe import FAMILY, MAX_IMAGE_BYTES, TOKEN_ENV, address_for, read_token, recv_json, send_json

log = logging.getLogger("ronda_helper")

REQUEST_TIMEOUT = 10.0
Capture = Callable[[int, int], tuple[bytes, int, int]]     # (max_width, max_bytes) → (jpeg, lebar, tinggi)


class HelperServer:
    def __init__(self, address: str, token: str, capture: Capture):
        self.capture = capture
        if FAMILY == "AF_UNIX":
            Path(address).unlink(missing_ok=True)
        # AF_PIPE: instance pertama dibuat dengan FILE_FLAG_FIRST_PIPE_INSTANCE → gagal bila nama sudah dipakai
        self.listener = Listener(address, family=FAMILY, authkey=token.encode())
        self.closed = threading.Event()

    def serve_forever(self) -> None:
        while not self.closed.is_set():
            try:
                conn = self.listener.accept()
            except AuthenticationError:
                log.warning("koneksi ke pipe ditolak: token salah")
                continue
            except OSError:
                if self.closed.is_set():
                    return
                log.exception("accept gagal")
                time.sleep(1)
                continue
            with conn:
                try:
                    self.handle(conn)
                except (EOFError, OSError, ValueError) as err:
                    log.warning("permintaan agent gagal diproses: %s", err)

    def handle(self, conn: Connection) -> None:
        request = recv_json(conn, REQUEST_TIMEOUT)
        op = request.get("op")
        if op == "ping":
            send_json(conn, {"ok": True, "version": __version__, "pid": os.getpid()})
        elif op == "screenshot":
            max_width = _int(request.get("max_width"), 1600, 320, 7680)
            max_bytes = _int(request.get("max_bytes"), MAX_IMAGE_BYTES, 64 * 1024, MAX_IMAGE_BYTES)
            try:
                content, width, height = self.capture(max_width, max_bytes)
            except Exception as err:
                log.warning("screenshot %s gagal: %s", request.get("request_id"), err)
                send_json(conn, {"ok": False, "error": str(err)[:200] or type(err).__name__})
                return
            send_json(conn, {"ok": True, "content_type": "image/jpeg", "width": width, "height": height})
            conn.send_bytes(content)
            log.info("screenshot %s: %dx%d, %d KB", request.get("request_id"), width, height, len(content) // 1024)
        else:
            send_json(conn, {"ok": False, "error": f"op tidak dikenal: {op!r}"[:200]})

    def close(self) -> None:
        self.closed.set()
        self.listener.close()


def _int(value, default: int, low: int, high: int) -> int:
    return min(high, max(low, value)) if isinstance(value, int) else default


def own_session() -> int:
    import ctypes
    from ctypes import wintypes
    session = wintypes.DWORD()
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    return session.value


def setup_logging() -> None:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Ronda" / "logs"
    handlers: list[logging.Handler] = []
    try:
        base.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(base / "helper.log", maxBytes=5 * 1024 * 1024,
                                                             backupCount=5, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:        # exe windowed (tanpa konsol) tidak punya stderr
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers or [logging.NullHandler()], force=True)


def main(argv: list[str] | None = None) -> int:
    from ronda_agent.config import default_data_dir
    parser = argparse.ArgumentParser(prog="ronda-helper")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--session", type=int, help="development; di Windows dibaca dari proses sendiri")
    args = parser.parse_args(argv)
    setup_logging()
    token = os.environ.pop(TOKEN_ENV, None) or read_token(args.data_dir)
    if not token:
        log.error("token pipe tidak ada (%s / local.token)", TOKEN_ENV)
        return 2
    session = own_session() if sys.platform == "win32" else (args.session or 0)
    address = address_for(args.data_dir, session)
    from ronda_helper.capture import screenshot
    try:
        server = HelperServer(address, token, screenshot)
    except OSError as err:
        log.error("pipe %s tidak bisa dibuat (helper lain sudah jalan?): %s", address, err)
        return 3
    log.info("ronda-helper %s melayani %s", __version__, address)
    server.serve_forever()
    return 0
