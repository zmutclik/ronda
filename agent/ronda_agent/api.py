"""Klien API server Ronda: satu fungsi per endpoint, kode respons dipetakan ke exception."""
import logging
import ssl
from pathlib import Path

import certifi
import httpx

from ronda_agent import __version__

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
LONG_TIMEOUT = httpx.Timeout(120.0, connect=5.0)    # upload screenshot, unduh rilis
RELEASE_PREFIX = "/api/agent/releases/"


class ApiError(Exception):
    status: int | None = None

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Unauthorized(ApiError):
    """401: token tidak ada/salah, atau PC ditolak/diblokir."""


class Forbidden(ApiError):
    """403: enrollment key tidak valid, atau PC masih pending."""


class NotFound(ApiError):
    """404: screenshot/rilis tidak ditemukan."""


class TooLarge(ApiError):
    """413."""


class Invalid(ApiError):
    """422: payload ditolak; jangan diulang."""


class Transient(ApiError):
    """5xx, timeout, atau jaringan putus: ulangi dengan backoff."""


_BY_STATUS = {401: Unauthorized, 403: Forbidden, 404: NotFound, 413: TooLarge, 422: Invalid}


def tls_context(ca_bundle: str | None) -> ssl.SSLContext:
    """CA internal RS bila ca_bundle diisi, selain itu bundle certifi. Bukan store Windows: store itu sering belum
    memuat root CA (Windows mengunduhnya sesuai kebutuhan, Python tidak memicunya). Dipakai API dan relay agar sama."""
    return ssl.create_default_context(cafile=ca_bundle or certifi.where())


def make_http_client(server_url: str, ca_bundle: str | None, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Verifikasi TLS selalu aktif (lihat tls_context)."""
    verify = tls_context(ca_bundle)
    if server_url.startswith("http://"):
        log.warning("server_url memakai http:// tanpa TLS — hanya untuk development")
    return httpx.Client(base_url=server_url, verify=verify, timeout=TIMEOUT, transport=transport,
                        headers={"User-Agent": f"ronda-agent/{__version__}"})


class Api:
    def __init__(self, client: httpx.Client, token: str | None = None):
        self.client = client
        self.token = token

    def _request(self, method: str, path: str, *, auth: bool = True, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self.client.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError as err:          # timeout, koneksi ditolak, TLS gagal
            raise Transient(f"{method} {path}: {type(err).__name__}: {err}") from err
        if resp.status_code < 300:
            return resp
        detail = _detail(resp)
        if resp.status_code >= 500:
            raise Transient(f"{method} {path}: {resp.status_code} {detail}", resp.status_code)
        cls = _BY_STATUS.get(resp.status_code, ApiError)
        raise cls(f"{method} {path}: {resp.status_code} {detail}", resp.status_code)

    def enroll(self, payload: dict) -> dict:
        return self._request("POST", "/api/agent/enroll", auth=False, json=payload).json()

    def heartbeat(self, payload: dict) -> dict:
        return self._request("POST", "/api/agent/heartbeat", json=payload).json()

    def metrics(self, samples: list[dict], disks: list[dict]) -> dict:
        return self._request("POST", "/api/agent/metrics", json={"samples": samples, "disks": disks}).json()

    def inventory(self, payload: dict) -> dict:
        return self._request("PUT", "/api/agent/inventory", json=payload).json()

    def apps(self, digest: str, apps: list[dict]) -> dict:
        return self._request("PUT", "/api/agent/apps", json={"hash": digest, "apps": apps}).json()

    def screenshot_fail(self, request_id: str, error: str) -> None:
        self._request("POST", f"/api/agent/screenshots/{request_id}/fail", json={"error": error[:200]})

    def screenshot_upload(self, request_id: str, content: bytes, content_type: str, width: int, height: int) -> None:
        self._request("POST", f"/api/agent/screenshots/{request_id}", content=content, timeout=LONG_TIMEOUT,
                      headers={"Content-Type": content_type, "X-Width": str(width), "X-Height": str(height)})

    def remote_fail(self, session_id: int, error: str) -> None:
        self._request("POST", f"/api/agent/remote/{int(session_id)}/fail", json={"error": error[:200]})

    def update_check(self) -> dict | None:
        return self._request("GET", "/api/agent/update").json().get("update")

    def download(self, path: str, dest: Path, max_bytes: int) -> None:
        """Unduh paket rilis ke `dest`. Hanya path relatif di bawah /api/agent/releases/: token Bearer tidak boleh
        terkirim ke host lain walau respons /update dimanipulasi."""
        if not path.startswith(RELEASE_PREFIX) or ".." in path or "//" in path:
            raise Invalid(f"url rilis tidak diizinkan: {path!r}")
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            with self.client.stream("GET", path, headers=headers, timeout=LONG_TIMEOUT) as resp:
                if resp.status_code >= 300:
                    resp.read()
                    cls = Transient if resp.status_code >= 500 else _BY_STATUS.get(resp.status_code, ApiError)
                    raise cls(f"GET {path}: {resp.status_code} {_detail(resp)}", resp.status_code)
                written = 0
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise TooLarge(f"paket melebihi {max_bytes} byte")
                        fh.write(chunk)
        except httpx.TransportError as err:
            raise Transient(f"GET {path}: {type(err).__name__}: {err}") from err


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    return str(body.get("detail", body))[:500] if isinstance(body, dict) else str(body)[:500]
