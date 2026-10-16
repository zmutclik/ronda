"""Metrik ringan lewat psutil: CPU, RAM, jaringan, disk, user login, boot, latency + IP ke server."""
import logging
import socket
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import psutil

from ronda_agent import schemas
from ronda_agent.schemas import iso

log = logging.getLogger(__name__)


class Sampler:
    def __init__(self, server_url: str):
        parts = urlsplit(server_url)
        self.server = (parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))
        self._net: tuple[float, int, int] | None = None
        psutil.cpu_percent(interval=None)      # panggilan pertama selalu 0.0: jadikan titik awal

    def sample(self, ts: datetime, latency_ms: float | None) -> dict:
        """Satu baris `metrics.samples`."""
        net_in, net_out = self._net_bps()
        return {"ts": iso(ts), "cpu_pct": schemas.pct(psutil.cpu_percent(interval=None)),
                "ram_pct": schemas.pct(psutil.virtual_memory().percent), "latency_ms": schemas.latency(latency_ms),
                "net_in_bps": net_in, "net_out_bps": net_out}

    def _net_bps(self) -> tuple[int | None, int | None]:
        counters = psutil.net_io_counters()
        now = time.monotonic()
        prev, self._net = self._net, (now, counters.bytes_recv, counters.bytes_sent)
        if prev is None or now <= prev[0]:
            return None, None
        seconds = now - prev[0]
        # counter bisa mundur (adaptor dilepas/reset): jangan kirim angka negatif
        return (max(0, int((counters.bytes_recv - prev[1]) * 8 / seconds)),
                max(0, int((counters.bytes_sent - prev[2]) * 8 / seconds)))

    def probe(self) -> tuple[float | None, str | None]:
        """TCP connect ke host:port server → (latency ms, IP lokal yang dipakai menuju server)."""
        start = time.perf_counter()
        try:
            with socket.create_connection(self.server, timeout=3) as sock:
                elapsed = (time.perf_counter() - start) * 1000
                return round(elapsed, 1), sock.getsockname()[0]
        except OSError as err:
            log.debug("probe %s:%s gagal: %s", *self.server, err)
            return None, None


def fixed_partitions() -> list[tuple[str, str]]:
    """(mount, path) partisi tetap, maks 26. Windows: "C:" (lewati CD-ROM, flashdisk, network drive)."""
    result: dict[str, str] = {}
    for part in psutil.disk_partitions(all=False):
        if sys.platform == "win32":
            if "fixed" not in part.opts:
                continue
            mount = part.device.rstrip("\\")     # "C:\\" → "C:"
        else:
            if part.fstype in ("squashfs", "") or len(part.mountpoint) > schemas.MOUNT_MAX:
                continue
            mount = part.mountpoint
        result.setdefault(mount, part.mountpoint)
        if len(result) >= schemas.DISKS_MAX:
            break
    return list(result.items())


def disk_usage() -> list[dict]:
    """Persen terpakai per partisi tetap: `{"mount": "C:", "used_pct": 71.2}`."""
    result = []
    for mount, path in fixed_partitions():
        try:
            result.append({"mount": mount, "used_pct": schemas.pct(psutil.disk_usage(path).percent)})
        except OSError:
            continue
    return result


def logged_user() -> str | None:
    """User sesi aktif (yang pertama; di PC klinis praktis hanya satu)."""
    try:
        users = psutil.users()
    except Exception:
        return None
    return schemas.text(users[0].name, schemas.USER_MAX) if users else None


def boot_time() -> datetime:
    return datetime.fromtimestamp(psutil.boot_time(), UTC).replace(microsecond=0)
