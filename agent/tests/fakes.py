"""Server palsu (httpx.MockTransport), kolektor palsu, dan jam palsu untuk test alur agent."""
import json
from datetime import UTC, datetime, timedelta

import httpx

from ronda_agent.schemas import iso


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.utc = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)

    def mono(self):
        return self.t

    def now(self):
        return self.utc

    def advance(self, seconds):
        self.t += seconds
        self.utc += timedelta(seconds=seconds)


class FakeCollectors:
    def __init__(self, clock):
        self.clock = clock

    def sample(self, ts, latency_ms):
        return {"ts": iso(ts), "cpu_pct": 12.5, "ram_pct": 61.0, "latency_ms": latency_ms,
                "net_in_bps": 1000, "net_out_bps": 500}

    def probe(self):
        return 3.4, "10.10.12.31"

    def disk_usage(self):
        return [{"mount": "C:", "used_pct": 71.2}]

    def logged_user(self):
        return "perawat.igd1"

    def boot_time(self):
        return datetime(2026, 10, 16, 0, 12, tzinfo=UTC)

    def hostname(self):
        return "IGD-PC03"

    def serial_number(self):
        return "PF2ABC12"

    apps_list = [{"name": "Google Chrome", "version": "129.0.6668", "publisher": "Google LLC",
                  "install_date": "2026-09-01"}]

    def inventory(self, route_ip):
        return {"os_name": "Windows 11 Pro 23H2", "os_build": "22631.4317", "ram_total_mb": 8192,
                "disks": [{"mount": "C:", "label": "OS", "media": "SSD", "total_bytes": 256, "used_bytes": 182}],
                "adapters": [{"name": "Ethernet", "ipv4": route_ip, "is_server_route": True, "is_up": True}]}

    def apps(self):
        return list(self.apps_list)


class FakeServer:
    """Meniru /api/agent/*: status host, perintah, dan kode error yang bisa diatur test."""

    def __init__(self):
        self.status = "pending"
        self.token = "tok-1"
        self.commands: list[dict] = []
        self.fail: dict[str, int] = {}          # path → status code paksaan
        self.calls: list[tuple[str, dict | None]] = []
        self.stored_samples: list[dict] = []
        self.fails: list[str] = []
        self.fail_reasons: list[str] = []
        self.uploads: list[tuple[str, dict, bytes]] = []
        self.update: dict | None = None
        self.packages: dict[str, bytes] = {}      # versi → byte zip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:           # unggah screenshot: byte gambar mentah
            body = None
        self.calls.append((path, body))
        if path in self.fail:
            return httpx.Response(self.fail[path], json={"detail": "paksa"})
        if path == "/api/agent/enroll":
            if body["enrollment_key"] != "IT-abcdefgh":
                return httpx.Response(403, json={"detail": "key"})
            return httpx.Response(200, json={"host_id": 42, "agent_token": self.token, "status": self.status})
        if request.headers.get("authorization") != f"Bearer {self.token}" or self.status == "rejected":
            return httpx.Response(401, json={"detail": "token"})
        if path == "/api/agent/heartbeat":
            return httpx.Response(200, json={"status": self.status, "commands": self.commands})
        if self.status == "pending":
            return httpx.Response(403, json={"detail": "pending"})
        if path == "/api/agent/metrics":
            self.stored_samples += body["samples"]
            return httpx.Response(200, json={"stored": len(body["samples"])})
        if path == "/api/agent/inventory":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/agent/apps":
            return httpx.Response(200, json={"changed": True})
        if path == "/api/agent/update":
            return httpx.Response(200, json={"update": self.update})
        if path.startswith("/api/agent/releases/"):
            version = path.split("/")[4]
            if version not in self.packages:
                return httpx.Response(404)
            return httpx.Response(200, content=self.packages[version])
        if path.endswith("/fail"):
            self.fails.append(path.split("/")[-2])
            self.fail_reasons.append(body["error"])
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/agent/screenshots/"):
            self.uploads.append((path.split("/")[-1], dict(request.headers), request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


def run_for(agent, clock, seconds, step=5):
    for _ in range(int(seconds / step)):
        agent.tick()
        clock.advance(step)
