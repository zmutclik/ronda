"""Saluran lokal agent (SYSTEM) ↔ helper (sesi user) untuk screenshot.

- Alamat: Windows `\\\\.\\pipe\\ronda-helper-<sesi>` (satu helper per sesi Windows; agent memilih sesi konsol aktif).
  Di luar Windows (development/test): socket Unix `helper-<sesi>.sock` di data dir.
- Autentikasi: `multiprocessing.connection` dengan authkey = isi `local.token` (challenge HMAC dua arah, token tidak
  pernah lewat saluran). Helper membuat pipe dengan FILE_FLAG_FIRST_PIPE_INSTANCE, dan di Windows agent juga mengecek
  bahwa exe pemilik pipe ada di `versions\\`, agar proses lain tidak bisa menyamar sebagai helper.
- Pesan: request = satu pesan JSON; balasan = header JSON, lalu (bila `ok`) satu pesan berisi byte gambar. Helper
  mengirim byte, bukan path file, agar agent (SYSTEM) tidak pernah membaca file yang ditunjuk proses user.

Modul ini dipakai agent, helper, dan launcher; jangan impor httpx/psutil di tingkat modul.
"""
import json
import os
import secrets
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import AuthenticationError, Client, Connection
from pathlib import Path

TOKEN_FILE = "local.token"
TOKEN_ENV = "RONDA_HELPER_TOKEN"        # launcher → helper lewat environment (tidak tampil di command line)
FAMILY = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
MAX_IMAGE_BYTES = 3 * 1024 * 1024       # sama dengan SCREENSHOT_MAX_BYTES server
MAX_HEADER_BYTES = 64 * 1024
RESPONSE_TIMEOUT = 15.0
IMAGE_MAGIC = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG"}
NO_SESSION = 0xFFFFFFFF


class HelperError(Exception):
    """Screenshot gagal; pesan dikirim apa adanya ke server (maks 200 karakter)."""


class HelperUnavailable(HelperError):
    """Tidak ada helper di sesi konsol (biasanya: tidak ada user login)."""


@dataclass(frozen=True)
class Shot:
    content: bytes
    content_type: str
    width: int
    height: int


def address_for(data_dir: Path, session: int) -> str:
    if sys.platform == "win32":
        return rf"\\.\pipe\ronda-helper-{session}"
    return str(data_dir / f"helper-{session}.sock")


def ensure_token(data_dir: Path) -> str:
    """Dipanggil launcher (SYSTEM). File mewarisi ACL data dir: hanya SYSTEM + Administrators."""
    path = data_dir / TOKEN_FILE
    try:
        token = path.read_text(encoding="ascii").strip()
        if len(token) >= 32:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_hex(32)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(token, encoding="ascii")
    os.replace(tmp, path)
    return token


def read_token(data_dir: Path) -> str | None:
    try:
        return (data_dir / TOKEN_FILE).read_text(encoding="ascii").strip() or None
    except FileNotFoundError:
        return None


def console_session() -> int | None:
    """Sesi yang sedang tampil di monitor fisik. Di luar Windows selalu 0 (development)."""
    if sys.platform != "win32":
        return 0
    import ctypes
    session = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    return None if session == NO_SESSION else int(session)


def pipe_server_exe(conn: Connection) -> str:
    """Windows: path exe proses yang membuat pipe."""
    import ctypes
    from ctypes import wintypes

    import psutil
    pid = wintypes.ULONG()
    if not ctypes.windll.kernel32.GetNamedPipeServerProcessId(wintypes.HANDLE(conn.fileno()), ctypes.byref(pid)):
        raise OSError("GetNamedPipeServerProcessId gagal")
    return psutil.Process(pid.value).exe()


def send_json(conn: Connection, message: dict) -> None:
    conn.send_bytes(json.dumps(message).encode())


def recv_json(conn: Connection, timeout: float | None = None) -> dict:
    if timeout is not None and not conn.poll(timeout):
        raise TimeoutError
    message = json.loads(conn.recv_bytes(MAX_HEADER_BYTES))
    if not isinstance(message, dict):
        raise ValueError("pesan bukan objek JSON")
    return message


class HelperClient:
    """Dipakai agent. `verify_server(conn)` menolak pipe milik proses lain; None = tanpa cek (development/test)."""

    def __init__(self, data_dir: Path, *, session: Callable[[], int | None] = console_session,
                 verify_server: Callable[[Connection], None] | None | str = "default",
                 timeout: float = RESPONSE_TIMEOUT):
        self.data_dir, self.session, self.timeout = data_dir, session, timeout
        if verify_server == "default":
            verify_server = self._verify_versions_dir if sys.platform == "win32" else None
        self.verify_server = verify_server

    def _verify_versions_dir(self, conn: Connection) -> None:
        exe = os.path.normcase(os.path.realpath(pipe_server_exe(conn)))
        root = os.path.normcase(os.path.realpath(self.data_dir / "versions")) + os.sep
        if not exe.startswith(root):
            raise HelperError(f"Pipe helper dimiliki proses asing ({Path(exe).name})")

    def screenshot(self, request_id: str, max_width: int = 1600) -> Shot:
        header, content = self._call({"op": "screenshot", "request_id": request_id, "max_width": max_width,
                                      "max_bytes": MAX_IMAGE_BYTES})
        content_type = str(header.get("content_type") or "")
        width, height = header.get("width"), header.get("height")
        magic = IMAGE_MAGIC.get(content_type)
        if not content or magic is None or not content.startswith(magic):
            raise HelperError("Helper mengirim data yang bukan gambar")
        if not (isinstance(width, int) and isinstance(height, int) and 0 < width < 65536 and 0 < height < 65536):
            raise HelperError("Helper mengirim ukuran gambar tidak valid")
        return Shot(content, content_type, width, height)

    def ping(self) -> dict:
        return self._call({"op": "ping"})[0]

    def _call(self, request: dict) -> tuple[dict, bytes | None]:
        session = self.session()
        if session is None:
            raise HelperUnavailable("Tidak ada user login")
        token = read_token(self.data_dir)
        if not token:
            raise HelperError(f"{TOKEN_FILE} tidak ada")
        # Seluruh percakapan di thread sendiri: jabat tangan auth tidak punya timeout, jadi helper yang macet tidak
        # boleh menahan agent selamanya.
        box: dict = {}

        def talk() -> None:
            try:
                box["result"] = self._talk(address_for(self.data_dir, session), token.encode(), request)
            except BaseException as err:     # noqa: B036 (diteruskan ke pemanggil)
                box["error"] = err

        worker = threading.Thread(target=talk, name="helper-pipe", daemon=True)
        worker.start()
        worker.join(self.timeout + 5)
        if worker.is_alive():
            raise HelperError("Helper screenshot tidak menjawab")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _talk(self, address: str, authkey: bytes, request: dict) -> tuple[dict, bytes | None]:
        try:
            conn = Client(address, family=FAMILY, authkey=authkey)
        except (FileNotFoundError, ConnectionRefusedError) as err:
            raise HelperUnavailable("Helper screenshot tidak berjalan di sesi user") from err
        except AuthenticationError as err:
            raise HelperError("Autentikasi ke helper gagal (token beda)") from err
        except OSError as err:
            raise HelperUnavailable(f"Helper screenshot tidak bisa dihubungi: {err}") from err
        with conn:
            if self.verify_server:
                try:
                    self.verify_server(conn)
                except HelperError:
                    raise
                except Exception as err:
                    raise HelperError(f"Pemilik pipe helper tidak bisa dicek: {err}") from err
            try:
                send_json(conn, request)
                header = recv_json(conn, self.timeout)
                if not header.get("ok"):
                    raise HelperError(str(header.get("error") or "Helper gagal tanpa keterangan"))
                if request["op"] != "screenshot":
                    return header, None
                if not conn.poll(self.timeout):
                    raise TimeoutError
                return header, conn.recv_bytes(MAX_IMAGE_BYTES)
            except TimeoutError as err:
                raise HelperError("Helper screenshot tidak menjawab") from err
            except (EOFError, OSError, ValueError) as err:
                raise HelperError(f"Saluran helper terputus: {err}") from err
