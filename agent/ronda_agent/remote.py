"""Sesi remote: tunnel WebSocket ke relay server Ronda ↔ UltraVNC di 127.0.0.1 PC ini.

Alur (perintah `remote` dari respons heartbeat):
1. Beri tahu user lewat helper (bila ada user login).
2. Tulis ultravnc.ini (password acak baru per sesi, hanya loopback) lalu jalankan service UltraVNC (`RondaVNC`).
3. Buka WebSocket ke `relay_path` server (token agent di header Authorization).
4. Jabat tangan RFB ke browser sebagai "server" dengan keamanan None, lalu login ke UltraVNC dengan password VNC
   sebagai "client". Password VNC tidak pernah meninggalkan PC: browser dan server hanya melihat byte setelah login.
   Mode lihat saja memakai password view-only UltraVNC (passwd2) + InputsEnabled=0, jadi input dari browser ditolak
   di PC walau noVNC diubah.
5. Salin byte dua arah sampai salah satu sisi tutup, lalu hentikan service UltraVNC.

Kegagalan sebelum tersambung dilaporkan ke server (`/api/agent/remote/<sesi>/fail`) dan tampil di halaman remote.
Bagian khusus Windows (service) ada di `UltraVnc`; sisanya diuji di Linux dengan VNC & relay palsu.
"""
import logging
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ronda_agent.api import Api, ApiError

log = logging.getLogger(__name__)

SERVICE = "RondaVNC"
VNC_PORT = 5939                 # bukan 5900: UltraVNC/TightVNC lain yang sudah terpasang di PC tidak bentrok
START_TIMEOUT = 20.0
BUF = 64 * 1024
RELAY_PREFIX = "/relay/agent/"
DISABLED_REASON = "Remote dinonaktifkan di PC ini"
MISSING_REASON = "UltraVNC tidak terpasang di PC ini (pasang ulang agent versi terbaru)"
RFB_38 = b"RFB 003.008\n"
SEC_NONE, SEC_VNC = 1, 2


class RemoteError(Exception):
    """Pesan dikirim apa adanya ke server (maks 200 karakter)."""


# ---------- DES ala VNC ----------

def _des(key: bytes, block: bytes) -> bytes:
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:                                          # cryptography < 43
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()   # K1=K2=K3 → DES tunggal
    return encryptor.update(block) + encryptor.finalize()


def _vnc_key(password: str) -> bytes:
    """VNC memakai bit tiap byte kunci dalam urutan terbalik dibanding DES standar."""
    raw = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in raw)


def vnc_response(password: str, challenge: bytes) -> bytes:
    key = _vnc_key(password)
    return _des(key, challenge[:8]) + _des(key, challenge[8:16])


# Kunci tetap VNC {23,82,107,6,35,78,88,7} dalam bentuk DES standar (bit terbalik), untuk password tersimpan
_STORED_KEY = bytes.fromhex("e84ad660c4721ae0")


def ini_password(password: str) -> str:
    """Nilai passwd/passwd2 di ultravnc.ini: format WritePrivateProfileStruct = hex 8 byte terenkripsi + 1 byte
    checksum (jumlah byte, mod 256)."""
    data = _des(_STORED_KEY, password.encode("latin-1", "replace")[:8].ljust(8, b"\0"))
    return (data + bytes([sum(data) & 0xFF])).hex().upper()


def new_password() -> str:
    return secrets.token_urlsafe(6)[:8]          # VNC hanya memakai 8 karakter pertama


# ---------- RFB ----------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise RemoteError("UltraVNC menutup koneksi saat jabat tangan")
        data += chunk
    return bytes(data)


def _reason(sock: socket.socket) -> str:
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    return _recv_exact(sock, min(length, 1024)).decode("latin-1", "replace")


def vnc_login(sock: socket.socket, password: str) -> None:
    """Sisi client ke UltraVNC sampai SecurityResult OK. Berikutnya ClientInit dari browser diteruskan apa adanya."""
    version = _recv_exact(sock, 12)
    if not version.startswith(b"RFB 003."):
        raise RemoteError(f"Bukan server VNC: {version!r}")
    minor = int(version[8:11]) if version[8:11].isdigit() else 3
    minor = 8 if minor >= 8 else 7 if minor == 7 else 3
    sock.sendall(b"RFB 003.%03d\n" % minor)
    if minor >= 7:
        count = _recv_exact(sock, 1)[0]
        if count == 0:
            raise RemoteError(f"UltraVNC menolak: {_reason(sock)}")
        types = _recv_exact(sock, count)
        chosen = SEC_VNC if SEC_VNC in types else SEC_NONE if SEC_NONE in types else None
        if chosen is None:
            raise RemoteError(f"Jenis keamanan UltraVNC tidak didukung: {list(types)}")
        sock.sendall(bytes([chosen]))
    else:
        (chosen,) = struct.unpack(">I", _recv_exact(sock, 4))
        if chosen == 0:
            raise RemoteError(f"UltraVNC menolak: {_reason(sock)}")
        if chosen not in (SEC_NONE, SEC_VNC):
            raise RemoteError(f"Jenis keamanan UltraVNC tidak didukung: {chosen}")
    if chosen == SEC_VNC:
        sock.sendall(vnc_response(password, _recv_exact(sock, 16)))
    if chosen == SEC_VNC or minor >= 8:
        (result,) = struct.unpack(">I", _recv_exact(sock, 4))
        if result != 0:
            detail = _reason(sock) if minor >= 8 else "password ditolak"
            raise RemoteError(f"Login ke UltraVNC gagal: {detail}")


class WsStream:
    """WebSocket (pesan biner) dibaca sebagai aliran byte; sisa setelah jabat tangan diteruskan ke VNC."""

    def __init__(self, ws):
        self.ws = ws
        self.buffer = bytearray()

    def recv_exact(self, n: int) -> bytes:
        while len(self.buffer) < n:
            message = self.ws.recv()
            if isinstance(message, bytes):
                self.buffer += message
        data, self.buffer = bytes(self.buffer[:n]), self.buffer[n:]
        return data

    def send(self, data: bytes) -> None:
        self.ws.send(data)


def serve_handshake(stream: WsStream) -> None:
    """Sisi server ke browser (noVNC): RFB 3.8, keamanan None. Menunggu sampai relay memasangkan browser."""
    stream.send(RFB_38)
    version = stream.recv_exact(12)
    if not version.startswith(b"RFB 003."):
        raise RemoteError(f"Versi RFB browser tidak dikenal: {version!r}")
    minor = int(version[8:11]) if version[8:11].isdigit() else 3
    if minor >= 7:
        stream.send(bytes([1, SEC_NONE]))
        if stream.recv_exact(1)[0] != SEC_NONE:
            raise RemoteError("Browser memilih jenis keamanan selain None")
        if minor >= 8:
            stream.send(struct.pack(">I", 0))
    else:
        stream.send(struct.pack(">I", SEC_NONE))


# ---------- UltraVNC (Windows) ----------

def default_uvnc_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    base = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
    return Path(base) / "Ronda" / "uvnc"


def ultravnc_ini(full_password: str, view_password: str, *, view_only: bool, port: int = VNC_PORT) -> str:
    """Hanya loopback, tanpa HTTP/Java viewer, tanpa transfer file, tray icon tampil (user tahu ada sesi), dan user
    tidak bisa membuka properti/mematikan server dari tray."""
    return "\n".join([
        "[ultravnc]",
        f"passwd={ini_password(full_password)}",
        f"passwd2={ini_password(view_password)}",
        "[admin]",
        "UseRegistry=0", "MSLogonRequired=0", "NewMSLogon=0", "UseDSMPlugin=0",
        "LoopbackOnly=1", "AllowLoopback=1", "AuthRequired=1", "AuthHosts=",
        "SocketConnect=1", "HTTPConnect=0", "AutoPortSelect=0", f"PortNumber={port}", f"HTTPPortNumber={port - 100}",
        "DisableTrayIcon=0", "AllowShutdown=0", "AllowProperties=0", "AllowEditClients=0", "AllowInjection=0",
        "FileTransferEnabled=0", "FTUserImpersonation=1", "BlankMonitorEnabled=0", "BlankInputsOnly=0",
        f"InputsEnabled={0 if view_only else 1}", "LocalInputsDisabled=0",
        "QuerySetting=2", "QueryTimeout=10", "QueryAccept=0", "QueryIfNoLogon=0",
        "IdleTimeout=0", "RemoveWallpaper=0", "DebugMode=0", "kickrdp=0", "service_commandline=",
        "[poll]",
        "TurboMode=1", "PollFullScreen=1", "EnableHook=1", "EnableDriver=0",
        "",
    ])


class UltraVnc:
    """Service UltraVNC (`winvnc.exe -service`, start manual) yang didaftarkan installer. Service-nya menjalankan
    winvnc di sesi user aktif, termasuk layar login dan UAC."""

    def __init__(self, folder: Path, *, port: int = VNC_PORT, service: str = SERVICE):
        self.folder, self.port, self.service = folder, port, service

    @property
    def installed(self) -> bool:
        return (self.folder / "winvnc.exe").is_file()

    def start(self, full_password: str, view_password: str, *, view_only: bool) -> None:
        import win32serviceutil
        self.stop()                              # ini dibaca saat start: sisa sesi sebelumnya dimatikan dulu
        ini = self.folder / "ultravnc.ini"
        tmp = ini.with_name(ini.name + ".tmp")
        tmp.write_text(ultravnc_ini(full_password, view_password, view_only=view_only), encoding="ascii")
        os.replace(tmp, ini)
        try:
            win32serviceutil.StartService(self.service)
        except Exception as err:
            raise RemoteError(f"Service {self.service} tidak bisa dijalankan: {err}") from err
        wait_port(self.port, START_TIMEOUT)

    def stop(self) -> None:
        import win32service
        import win32serviceutil
        try:
            status = win32serviceutil.QueryServiceStatus(self.service)[1]
            if status != win32service.SERVICE_STOPPED:
                win32serviceutil.StopService(self.service)
        except Exception as err:
            log.warning("service %s tidak bisa dihentikan: %s", self.service, err)


def wait_port(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise RemoteError(f"UltraVNC tidak mendengarkan port {port} dalam {timeout:.0f} dtk") from None
            time.sleep(0.5)


# ---------- tunnel ----------

def open_relay(server_url: str, ca_bundle: str | None, token: str, path: str):
    """WebSocket ke relay. Hanya path di bawah /relay/agent/ server yang sama: token tidak boleh terkirim ke tempat
    lain walau perintah dari server dimanipulasi."""
    from websockets.sync.client import connect

    from ronda_agent import __version__
    if not path.startswith(RELAY_PREFIX) or ".." in path or "//" in path or "?" in path:
        raise RemoteError(f"Alamat relay tidak diizinkan: {path[:60]!r}")
    secure = server_url.startswith("https://")
    url = ("wss://" if secure else "ws://") + server_url.split("://", 1)[1].rstrip("/") + path
    tls = (ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()) \
        if secure else None
    return connect(url, ssl=tls, open_timeout=15, close_timeout=5, max_size=4 * 1024 * 1024,
                   additional_headers={"Authorization": f"Bearer {token}"},
                   user_agent_header=f"ronda-agent/{__version__}")


def pump(ws, stream: WsStream, sock: socket.socket) -> None:
    """Salin dua arah sampai salah satu sisi tutup. Sisa buffer jabat tangan (mis. ClientInit) dikirim dulu."""
    if stream.buffer:
        sock.sendall(bytes(stream.buffer))
        stream.buffer.clear()

    def ws_to_vnc() -> None:
        try:
            for message in ws:
                if isinstance(message, bytes):
                    sock.sendall(message)
        except Exception as err:                 # koneksi tertutup di salah satu sisi
            log.debug("ws→vnc selesai: %s", err)
        finally:
            _shutdown(sock)

    reader = threading.Thread(target=ws_to_vnc, name="remote-ws", daemon=True)
    reader.start()
    try:
        while data := sock.recv(BUF):
            ws.send(data)
    except Exception as err:
        log.debug("vnc→ws selesai: %s", err)
    finally:
        ws.close()
        _shutdown(sock)
        reader.join(10)


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class RemoteJob:
    """Satu sesi remote di thread sendiri (bisa berjam-jam), loop utama agent tetap jalan.
    `result`: None (berjalan) | "done"."""

    def __init__(self, api: Api, session_id: int, *, mode: str, user: str, relay_path: str,
                 vnc: UltraVnc | None, connect: Callable[[str], object], notify: Callable[[str], None] | None,
                 enabled: bool = True):
        self.api, self.session_id, self.mode, self.user, self.relay_path = api, session_id, mode, user, relay_path
        self.vnc, self.connect, self.notify, self.enabled = vnc, connect, notify, enabled
        self.result: str | None = None
        self.thread = threading.Thread(target=self._run, name=f"remote-{session_id}", daemon=True)

    def start(self) -> "RemoteJob":
        self.thread.start()
        return self

    def _run(self) -> None:
        try:
            self._work()
        except Exception:
            log.exception("remote %s: galat tak terduga", self.session_id)
        self.result = "done"

    def _work(self) -> None:
        if not self.enabled:
            return self._fail(DISABLED_REASON)
        if self.vnc is None:
            return self._fail(MISSING_REASON)
        view_only = self.mode != "control"
        full, view = new_password(), new_password()
        self._tell(_remote_message(self.user, self.mode))
        try:
            self.vnc.start(full, view, view_only=view_only)
        except RemoteError as err:
            self.vnc.stop()
            return self._fail(str(err))
        try:
            self._tunnel(view if view_only else full)
        finally:
            self.vnc.stop()
            self._tell("Sesi remote oleh Tim IT telah selesai.")
            log.info("remote %s selesai", self.session_id)

    def _tunnel(self, password: str) -> None:
        try:
            ws = self.connect(self.relay_path)
        except Exception as err:
            return self._fail(f"Relay tidak terjangkau: {err}")
        log.info("remote %s: tersambung ke relay, menunggu browser", self.session_id)
        with ws:
            stream = WsStream(ws)
            try:
                serve_handshake(stream)          # relay menahan agent sampai browser datang (atau menutupnya)
            except RemoteError as err:
                return self._fail(str(err))
            except Exception as err:             # relay menutup: browser tidak datang / sesi dihentikan
                log.info("remote %s: relay menutup sebelum browser tersambung (%s)", self.session_id, err)
                return
            try:
                sock = socket.create_connection(("127.0.0.1", self.vnc.port), timeout=10)
            except OSError as err:
                return self._fail(f"UltraVNC tidak bisa dihubungi: {err}")
            with sock:
                try:
                    vnc_login(sock, password)
                except (RemoteError, OSError) as err:
                    return self._fail(str(err))
                sock.settimeout(None)
                log.info("remote %s: sesi %s berjalan", self.session_id, self.mode)
                pump(ws, stream, sock)

    def _fail(self, error: str) -> None:
        log.warning("remote %s gagal: %s", self.session_id, error)
        try:
            self.api.remote_fail(self.session_id, error)
        except ApiError as err:                  # sesi sudah selesai / server tidak terjangkau: cukup dicatat
            log.info("lapor gagal remote %s tidak diterima: %s", self.session_id, err)

    def _tell(self, text: str) -> None:
        if not self.notify:
            return
        try:
            self.notify(text)
        except Exception as err:                 # tidak ada user login / helper mati: sesi tetap jalan
            log.info("pemberitahuan remote tidak tampil: %s", err)


def _remote_message(user: str, mode: str) -> str:
    action = "mengendalikan" if mode == "control" else "melihat"
    return (f"Tim IT ({user[:50]}) mulai {action} layar komputer ini pukul {time.strftime('%H:%M')}. "
            "Sesi remote tercatat di Audit Log Ronda.")
