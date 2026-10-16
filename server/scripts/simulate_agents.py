"""Simulator agent untuk development/demo (BUKAN untuk production).
Membuat folder contoh + enrollment key, mendaftarkan PC palsu lewat API agent asli, menyetujuinya,
lalu mengirim inventaris, aplikasi, backfill metrik 24 jam, dan heartbeat berkala.

    uv run python scripts/simulate_agents.py --url http://127.0.0.1:8000 --loop 600
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import random
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents import service as agent_service
from app.auth.audit import SYSTEM, Actor
from app.auth.models import User
from app.core.database import make_engine
from app.core.discovery import import_domain_modules
from app.folders import service as folder_service
from app.folders.models import Folder

import_domain_modules("models")

FOLDERS = {
    ("IGD", None): [("Triase", "10.10.12.0/24"), ("Resusitasi", "10.10.13.0/24"), ("Observasi", "10.10.14.0/24")],
    ("Rawat Jalan", None): [("Poli Anak", "10.10.20.0/24"), ("Poli Dalam", "10.10.21.0/24")],
    ("Rawat Inap", None): [("Anggrek", "10.10.22.0/24"), ("Melati", "10.10.23.0/24")],
    ("Penunjang", None): [("Farmasi", "10.10.15.0/24"), ("Radiologi", "10.10.18.0/24"), ("Laboratorium", "10.10.16.0/24")],
    ("Administrasi", None): [("Pendaftaran", "10.10.11.0/24"), ("Kasir", "10.10.17.0/24")],
    ("Instalasi IT", "10.10.30.0/24"): [],
}
PREFIX = {"Triase": "IGD", "Resusitasi": "IGD", "Observasi": "IGD", "Poli Anak": "POL", "Poli Dalam": "POL",
          "Anggrek": "ANG", "Melati": "MEL", "Farmasi": "FAR", "Radiologi": "RAD", "Laboratorium": "LAB",
          "Pendaftaran": "PDF", "Kasir": "KSR", "Instalasi IT": "IT"}
USERS = ["perawat.igd1", "perawat.igd2", "dr.hendra", "dr.yuni", "analis.sri", "rad.budi", "kasir.andi", "loket7",
         "apoteker.rina", None]
MODELS = [("Lenovo", "ThinkCentre M70s", "Intel Core i3-10100", 4, 8), ("Dell", "OptiPlex 3080", "Intel Core i5-10500", 6, 12),
          ("HP", "ProDesk 400 G4", "Intel Core i3-7100", 2, 4)]
APPS = [("SIMRS Client", "4.2.1", "RS Contoh"), ("Google Chrome", "129.0.6668", "Google LLC"),
        ("Microsoft Office Standard 2019", "16.0.10417", "Microsoft"), ("Adobe Acrobat Reader", "24.003.20112", "Adobe"),
        ("Java 8 Update 401", "8.0.4010", "Oracle"), ("AnyDesk", "8.1.0", "AnyDesk Software"),
        ("7-Zip", "23.01 (x64)", "Igor Pavlov"), ("Ronda Agent", "1.3.0", "Tim IT")]
# Hostname → (kondisi khusus) agar dashboard/alert punya isi
SPECIAL = {"IGD-PC03": "disk", "RAD-PC01": "cpu", "LAB-PC05": "ram", "PDF-PC07": "latency", "FAR-PC02": "offline"}


class FakePC:
    def __init__(self, hostname: str, ip: str, rng: random.Random):
        self.hostname, self.ip, self.rng = hostname, ip, rng
        self.kind = SPECIAL.get(hostname)
        self.model = rng.choice(MODELS)
        self.ram_gb = rng.choice([4, 8, 8, 16])
        self.user = rng.choice(USERS)
        self.boot = datetime.now(UTC) - timedelta(minutes=rng.randint(30, 600))
        self.token: str | None = None
        self.disk_c = 94.0 if self.kind == "disk" else rng.uniform(35, 80)
        self.version = "1.3.0" if rng.random() < .7 else "1.2.4"

    def sample(self, ts: datetime) -> dict:
        hour = ts.astimezone(UTC).hour + 7
        busy = 1.0 if 8 <= hour % 24 <= 15 else 0.4
        cpu = min(100, max(1, self.rng.gauss(30 * busy, 10)))
        ram = min(100, max(5, self.rng.gauss(55, 6)))
        latency = max(1, self.rng.gauss(6, 2))
        recent = datetime.now(UTC) - ts < timedelta(minutes=25)
        if self.kind == "cpu" and recent:
            cpu = self.rng.uniform(96, 99.5)
        if self.kind == "ram" and recent:
            ram = self.rng.uniform(91, 94)
        if self.kind == "latency" and recent:
            latency = self.rng.uniform(300, 520)
        return {"ts": ts.isoformat(), "cpu_pct": round(cpu, 1), "ram_pct": round(ram, 1), "latency_ms": round(latency, 1),
                "net_in_bps": int(self.rng.uniform(1e4, 2e6) * busy), "net_out_bps": int(self.rng.uniform(5e3, 4e5) * busy)}

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


async def setup_db(count: int) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Folder + enrollment key langsung lewat service (butuh DATABASE_URL di .env)."""
    engine = make_engine(pool_size=1, max_overflow=0, statement_timeout=60)
    factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        async with factory() as db:
            async with db.begin():
                admin = await db.scalar(select(User).where(User.role == "admin").limit(1))
                existing = {f.name for f in await db.scalars(select(Folder))}
            actor = Actor(admin.id, admin.username, "127.0.0.1") if admin else SYSTEM
            for (unit, subnet), rooms in FOLDERS.items():
                if unit in existing:
                    continue
                parent = await folder_service.create_folder(db, name=unit, parent_id=None, location=None,
                                                            subnet_hint=subnet, actor=actor)
                for room, room_subnet in rooms:
                    await folder_service.create_folder(db, name=room, parent_id=parent.id, location=None,
                                                       subnet_hint=room_subnet, actor=actor)
            _, key = await agent_service.create_enrollment_key(db, prefix="RS-2026-Q4", valid_days=90, actor=actor)
    finally:
        await engine.dispose()
    pcs = []
    rooms = [(room, subnet) for rs in FOLDERS.values() for room, subnet in rs] + [("Instalasi IT", "10.10.30.0/24")]
    per_prefix: dict[str, int] = {}
    for n in range(count):
        room, subnet = rooms[n % len(rooms)]
        prefix = PREFIX[room]
        per_prefix[prefix] = per_prefix.get(prefix, 0) + 1
        idx = per_prefix[prefix]
        pcs.append((f"{prefix}-PC{idx:02d}", f"{subnet.rsplit('.', 1)[0]}.{40 + idx}"))
    pending = [("DESKTOP-7H2KQ1", "10.10.22.57"), ("PC-BARU-LT2", "10.10.11.80"), ("LAPTOP-DIR01", "10.10.30.5")]
    return key, pcs, pending


async def approve_all(hostnames: set[str]) -> None:
    engine = make_engine(pool_size=1, max_overflow=0, statement_timeout=60)
    factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        async with factory() as db:
            async with db.begin():
                tree = await folder_service.load_tree(db)
                pending = await agent_service.pending_hosts(db, tree)
            choices = {p.id: p.suggestion_id for p in pending if p.hostname in hostnames}
            if choices:
                await agent_service.approve_hosts(db, choices, tree, SYSTEM)
    finally:
        await engine.dispose()


async def send_inventory(client: httpx.AsyncClient, pc: FakePC) -> None:
    maker, model, cpu, cores, threads = pc.model
    await client.put("/api/agent/inventory", headers=pc.headers(), json={
        "os_name": "Windows 10 Pro 22H2" if pc.rng.random() < .7 else "Windows 11 Pro 23H2", "os_build": "19045.4894",
        "cpu_model": cpu, "cpu_cores": cores, "cpu_threads": threads, "ram_total_mb": pc.ram_gb * 1024,
        "manufacturer": maker, "model": model, "serial_number": hashlib.md5(pc.hostname.encode()).hexdigest()[:8].upper(),
        "disks": [{"mount": "C:", "label": "Sistem", "media": "SSD", "total_bytes": 238 * 10**9,
                   "used_bytes": int(238 * 10**9 * pc.disk_c / 100)},
                  {"mount": "D:", "label": "Data", "media": "HDD", "total_bytes": 465 * 10**9, "used_bytes": 190 * 10**9}],
        "adapters": [
            {"name": "Ethernet", "description": "Realtek PCIe GbE Family Controller", "mac": "3C:52:82:1A:7F:0E",
             "is_up": True, "is_server_route": True, "speed_mbps": 1000, "ipv4": pc.ip, "prefix_len": 24,
             "gateway": pc.ip.rsplit(".", 1)[0] + ".1", "dns": "10.10.1.10, 10.10.1.11", "dhcp": True,
             "rx_bps": 1_200_000, "tx_bps": 300_000},
            {"name": "Wi-Fi", "description": "Intel Wi-Fi 6 AX201", "mac": "A4:C3:F0:5D:21:9B", "is_up": pc.rng.random() < .3,
             "ssid": "RS-Staff", "signal_pct": 72, "ipv4": "192.168.50.118", "prefix_len": 24, "dhcp": True},
            {"name": "vEthernet (Default Switch)", "description": "Hyper-V Virtual Ethernet Adapter", "is_virtual": True,
             "is_up": True, "ipv4": "172.24.80.1", "prefix_len": 20, "dhcp": False, "mac": "00:15:5D:01:6E:00"},
        ],
    })
    apps = [{"name": n, "version": v, "publisher": p, "install_date": "2026-09-02"} for n, v, p in APPS]
    digest = hashlib.sha256(repr(apps).encode()).hexdigest()
    await client.put("/api/agent/apps", headers=pc.headers(), json={"hash": digest, "apps": apps})


async def heartbeat(client: httpx.AsyncClient, pc: FakePC) -> None:
    s = pc.sample(datetime.now(UTC))
    resp = await client.post("/api/agent/heartbeat", headers=pc.headers(), json={
        "ts": s["ts"], "agent_version": pc.version, "update_status": "installed",
        "ip": pc.ip, "cpu_pct": s["cpu_pct"], "ram_pct": s["ram_pct"], "latency_ms": s["latency_ms"],
        "disks": [{"mount": "C:", "used_pct": pc.disk_c}, {"mount": "D:", "used_pct": 41}],
        "logged_user": pc.user, "boot_at": pc.boot.isoformat(),
    })
    resp.raise_for_status()
    for cmd in resp.json().get("commands", []):
        if cmd["type"] == "screenshot":   # gambar contoh 1×1 JPEG
            jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9")
            await client.post(f"/api/agent/screenshots/{cmd['request_id']}", headers={**pc.headers(), "X-Width": "1600",
                              "X-Height": "900", "Content-Type": "image/jpeg"}, content=jpeg)
        elif cmd["type"] == "refresh_inventory":
            await send_inventory(client, pc)


async def backfill(client: httpx.AsyncClient, pc: FakePC, hours: int) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    samples = [pc.sample(now - timedelta(minutes=m)) for m in range(hours * 60, 0, -1)]
    disks = [{"ts": (now - timedelta(minutes=m)).isoformat(), "mount": "C:",
              "used_pct": round(pc.disk_c - m / (60 * 24) * (1.1 if pc.kind == "disk" else .2), 2)}
             for m in range(hours * 60, 0, -5)]
    for i in range(0, len(samples), 500):
        (await client.post("/api/agent/metrics", headers=pc.headers(), json={"samples": samples[i:i + 500]})).raise_for_status()
    for i in range(0, len(disks), 500):
        (await client.post("/api/agent/metrics", headers=pc.headers(), json={"disks": disks[i:i + 500]})).raise_for_status()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--backfill-hours", type=int, default=24)
    parser.add_argument("--loop", type=int, default=0, help="detik heartbeat berkala setelah setup (0 = selesai)")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    rng = random.Random(7)
    state = Path("data/simulator.json")       # token agent palsu: jalankan ulang tanpa mendaftar lagi

    if state.exists():
        saved = json.loads(state.read_text())
        pcs = [FakePC(h, ip, random.Random(rng.random())) for h, ip, _ in saved]
        for pc, (_, _, token) in zip(pcs, saved, strict=True):
            pc.token = token
        async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
            await _loop(client, pcs, args)
        return

    key, specs, pending = await setup_db(args.count)
    pcs = [FakePC(h, ip, random.Random(rng.random())) for h, ip in specs]
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        for pc in pcs + [FakePC(h, ip, rng) for h, ip in pending]:
            resp = await client.post("/api/agent/enroll", json={"enrollment_key": key, "hostname": pc.hostname,
                                                                "agent_version": "1.3.0", "ip": pc.ip})
            resp.raise_for_status()
            pc.token = resp.json()["agent_token"]
        await approve_all({pc.hostname for pc in pcs})
        for pc in pcs:
            await send_inventory(client, pc)
            if args.backfill_hours:
                await backfill(client, pc, args.backfill_hours)
            await heartbeat(client, pc)
        print(f"{len(pcs)} PC terdaftar & disetujui, {len(pending)} menunggu persetujuan. Key: {key}")
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps([(pc.hostname, pc.ip, pc.token) for pc in pcs]))
        await _loop(client, pcs, args)


async def _loop(client: httpx.AsyncClient, pcs: list[FakePC], args) -> None:
    elapsed = 0
    while elapsed < args.loop:
        await asyncio.sleep(args.interval)
        elapsed += args.interval
        for pc in pcs:
            if pc.kind == "offline":
                continue
            await heartbeat(client, pc)
            await client.post("/api/agent/metrics", headers=pc.headers(),
                              json={"samples": [pc.sample(datetime.now(UTC))]})


if __name__ == "__main__":
    asyncio.run(main())
