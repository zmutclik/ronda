"""Penulisan data kiriman agent. Dipanggil router API agent (app/agents) setelah token diverifikasi.
Setiap fungsi pemilik transaksinya sendiri dan menulis secara batch (multi-row INSERT), bukan per sampel."""
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth.models import User
from app.core.config import settings
from app.core.database import db_now_plus
from app.hosts.models import (
    DiskMetric, Host, HostDisk, HostEvent, HostLatest, InstalledApp, Metric, NetworkAdapter, RemoteSession,
    Screenshot,
)
from app.hosts.schemas import AppsIn, HeartbeatIn, InventoryIn, MetricsIn
from app.settings.service import get_value

logger = logging.getLogger(__name__)

# Sampel di luar rentang ini dibuang: jam PC yang salah tidak boleh mengisi partisi lama/masa depan
MAX_SAMPLE_AGE = timedelta(days=2)
MAX_CLOCK_SKEW = timedelta(minutes=5)
# Perintah screenshot yang belum diambil agent dalam waktu ini dianggap gagal (hosts/tasks.py)
SCREENSHOT_PICKUP_WINDOW = 10 * 60
# Perintah remote dititipkan selama ini; relay menerima agent selama itu juga (app/remote/service.PICKUP_WINDOW)
REMOTE_PICKUP_WINDOW = 120
MAX_APP_EVENTS = 8


def _event(host_id: int, kind: str, message: str, level: str | None = None, meta: dict | None = None) -> HostEvent:
    return HostEvent(host_id=host_id, kind=kind, level=level, message=message[:255], meta=meta)


async def heartbeat(db: AsyncSession, host_id: int, data: HeartbeatIn, ip: str) -> dict:
    """Tanda hidup + kondisi terakhir. Mengembalikan status host dan perintah yang menunggu."""
    disk_max = max(data.disks, key=lambda d: d.used_pct, default=None)
    commands: list[dict] = []
    async with db.begin():
        host = await db.get(Host, host_id, with_for_update=True)
        host.last_seen_at = func.now()
        host.last_ip = data.ip or ip
        if host.status == "pending":
            return {"status": "pending", "commands": []}       # belum disetujui: data belum disimpan

        events = []
        if host.status == "offline":
            events.append(_event(host_id, "status", "Kembali online", "ok"))
        host.status = "online"
        if data.agent_version and host.agent_version and data.agent_version != host.agent_version:
            events.append(_event(host_id, "agent", f"Agent diperbarui {host.agent_version} → {data.agent_version}",
                                 "ok", {"from": host.agent_version, "to": data.agent_version}))
        if data.update_status == "rollback" and host.update_status != "rollback":
            events.append(_event(host_id, "agent", "Update agent gagal, kembali ke versi sebelumnya", "warn"))
        host.agent_version = data.agent_version or host.agent_version
        host.update_status = data.update_status or host.update_status

        latest = await db.get(HostLatest, host_id, with_for_update=True)
        if latest is None:
            latest = HostLatest(host_id=host_id)
            db.add(latest)
        if data.logged_user and data.logged_user != latest.logged_user:
            events.append(_event(host_id, "login", f"{data.logged_user} login", "ok", {"user": data.logged_user}))
            latest.logged_since = func.now()
        if data.boot_at and (latest.boot_at is None or abs(data.boot_at - latest.boot_at) > timedelta(minutes=2)):
            events.append(_event(host_id, "agent", "PC menyala, agent tersambung", "ok"))
        latest.ts = func.now()
        latest.cpu_pct, latest.ram_pct = data.cpu_pct, data.ram_pct
        latest.disk_max_pct = disk_max.used_pct if disk_max else 0
        latest.disk_max_mount = disk_max.mount if disk_max else None
        latest.latency_ms = data.latency_ms
        latest.logged_user = data.logged_user
        latest.boot_at = data.boot_at
        db.add_all(events)

        shots = await db.execute(
            select(Screenshot.request_uuid, Screenshot.notify_user).where(
                Screenshot.host_id == host_id, Screenshot.status == "requested",
                Screenshot.requested_at > db_now_plus(-SCREENSHOT_PICKUP_WINDOW),
            )
        )
        # notify: helper menampilkan pemberitahuan di layar user setelah gambar diambil
        commands += [{"type": "screenshot", "request_id": rid, "notify": notify} for rid, notify in shots]
        # Sesi remote yang menunggu agent (app/remote): agent membuka tunnel ke relay. Diulang tiap heartbeat sampai
        # tersambung/selesai; agent mengabaikan sesi yang sudah ditanganinya.
        remotes = await db.execute(
            select(RemoteSession.id, RemoteSession.mode, RemoteSession.reason, User.username)
            .join(User, User.id == RemoteSession.user_id)
            .where(RemoteSession.host_id == host_id, RemoteSession.ended_at.is_(None),
                   RemoteSession.connected_at.is_(None),
                   RemoteSession.started_at > db_now_plus(-REMOTE_PICKUP_WINDOW))
        )
        commands += [{"type": "remote", "session_id": sid, "mode": mode, "user": user, "reason": reason,
                      "relay_path": f"/relay/agent/{sid}"} for sid, mode, reason, user in remotes]
        if host.inventory_requested_at is not None:
            commands.append({"type": "refresh_inventory"})
            host.inventory_requested_at = None
    return {"status": "online", "commands": commands}


def _valid_ts(ts: datetime, now: datetime) -> bool:
    return now - MAX_SAMPLE_AGE <= ts <= now + MAX_CLOCK_SKEW


async def store_metrics(db: AsyncSession, host_id: int, data: MetricsIn) -> int:
    """Batch sampel → metrics/disk_metrics. Sampel ganda (kirim ulang dari buffer agent) menimpa yang lama."""
    now = datetime.now(UTC).replace(tzinfo=None)              # hanya untuk menyaring jam PC yang ngawur
    rows = [{"host_id": host_id, **s.model_dump()} for s in data.samples if _valid_ts(s.ts, now)]
    disk_rows = [{"host_id": host_id, **d.model_dump()} for d in data.disks if _valid_ts(d.ts, now)]
    async with db.begin():
        if rows:
            stmt = insert(Metric).values(rows)
            await db.execute(stmt.on_duplicate_key_update(
                cpu_pct=stmt.inserted.cpu_pct, ram_pct=stmt.inserted.ram_pct, latency_ms=stmt.inserted.latency_ms,
                net_in_bps=stmt.inserted.net_in_bps, net_out_bps=stmt.inserted.net_out_bps,
            ))
        if disk_rows:
            stmt = insert(DiskMetric).values(disk_rows)
            await db.execute(stmt.on_duplicate_key_update(used_pct=stmt.inserted.used_pct))
    return len(rows) + len(disk_rows)


async def store_inventory(db: AsyncSession, host_id: int, data: InventoryIn) -> None:
    specs = data.model_dump(exclude={"disks", "adapters"}, exclude_none=True)
    async with db.begin():
        manual = await db.scalar(select(Host.manual_specs).where(Host.id == host_id))
        for field in manual or ():
            specs.pop(field, None)               # diubah manual di tab Aksi: data agent tidak menimpa
        if specs:
            await db.execute(update(Host).where(Host.id == host_id).values(**specs))

        mounts = [d.mount for d in data.disks]
        await db.execute(delete(HostDisk).where(HostDisk.host_id == host_id, HostDisk.mount.not_in(mounts)))
        if data.disks:
            stmt = insert(HostDisk).values(
                [{"host_id": host_id, "updated_at": func.now(), **d.model_dump()} for d in data.disks])
            await db.execute(stmt.on_duplicate_key_update(
                label=stmt.inserted.label, media=stmt.inserted.media, total_bytes=stmt.inserted.total_bytes,
                used_bytes=stmt.inserted.used_bytes, updated_at=stmt.inserted.updated_at,
            ))

        names = [a.name for a in data.adapters]
        await db.execute(delete(NetworkAdapter).where(
            NetworkAdapter.host_id == host_id, NetworkAdapter.name.not_in(names)))
        if data.adapters:
            rows = []
            for a in data.adapters:
                row = {"host_id": host_id, "updated_at": func.now(), **a.model_dump()}
                row["last_up_at"] = func.now() if a.is_up else None
                rows.append(row)
            stmt = insert(NetworkAdapter).values(rows)
            columns = [c for c in rows[0] if c not in ("host_id", "name", "last_up_at")]
            await db.execute(stmt.on_duplicate_key_update(
                # last_up_at hanya diperbarui saat adapter sedang terhubung ("Terakhir aktif")
                last_up_at=func.if_(stmt.inserted.is_up, stmt.inserted.last_up_at, NetworkAdapter.last_up_at),
                **{c: stmt.inserted[c] for c in columns},
            ))


async def store_apps(db: AsyncSession, host_id: int, data: AppsIn) -> bool:
    """Ganti daftar aplikasi bila hash berubah; perubahan dicatat di Riwayat. False bila tidak ada perubahan."""
    incoming = {(a.name, a.version): a for a in data.apps}     # buang duplikat (UNIQUE host, name, version)
    async with db.begin():
        host = await db.get(Host, host_id, with_for_update=True)
        if host.apps_hash == data.hash:
            return False
        current = {(n, v) for n, v in (await db.execute(
            select(InstalledApp.name, InstalledApp.version).where(InstalledApp.host_id == host_id))).all()}
        removed, added = current - incoming.keys(), incoming.keys() - current
        if removed:
            await db.execute(delete(InstalledApp).where(
                InstalledApp.host_id == host_id,
                tuple_(InstalledApp.name, InstalledApp.version).in_(list(removed)),
            ))
        if added:
            # UNIQUE (host, name, version) memakai collation _ci: "Foo"/"foo" (atau beda aksen) berbeda di Python
            # tetapi sama di DB. Tanpa ON DUPLICATE, payload seperti itu → IntegrityError → 500 → agent kirim ulang
            # selamanya. Baris kembar cukup digabung (yang terakhir menang).
            stmt = insert(InstalledApp).values([{"host_id": host_id, **incoming[key].model_dump()} for key in added])
            await db.execute(stmt.on_duplicate_key_update(
                publisher=stmt.inserted.publisher, install_date=stmt.inserted.install_date))

        if host.apps_hash is None:
            db.add(_event(host_id, "app", f"Daftar aplikasi diterima ({len(incoming)} aplikasi)"))
        else:
            db.add_all(_app_events(host_id, removed, added))
        host.apps_hash = data.hash
    return True


def _app_events(host_id: int, removed: set, added: set) -> list[HostEvent]:
    old = {name: ver for name, ver in removed}
    new = {name: ver for name, ver in added}
    messages = [f"Aplikasi berubah: {n} {old[n]} → {new[n]}".strip() for n in sorted(old.keys() & new.keys())]
    messages += [f"Aplikasi dipasang: {n} {new[n]}".strip() for n in sorted(new.keys() - old.keys())]
    messages += [f"Aplikasi dihapus: {n}" for n in sorted(old.keys() - new.keys())]
    events = [_event(host_id, "app", m) for m in messages[:MAX_APP_EVENTS]]
    if len(messages) > MAX_APP_EVENTS:
        events.append(_event(host_id, "app", f"… dan {len(messages) - MAX_APP_EVENTS} perubahan aplikasi lain"))
    return events


IMAGE_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}


class ScreenshotRejected(ValueError):
    pass


def _image_ext(content: bytes) -> str:
    for magic, ext in IMAGE_TYPES.items():
        if content.startswith(magic) and (ext != ".webp" or content[8:12] == b"WEBP"):
            return ext
    raise ScreenshotRejected("Format gambar tidak dikenal (JPEG/PNG/WebP).")


def _write_file(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


async def store_screenshot(
    db: AsyncSession, host_id: int, request_uuid: str, content: bytes, width: int | None, height: int | None,
) -> None:
    ext = _image_ext(content)
    async with db.begin():
        shot_id = await db.scalar(select(Screenshot.id).where(
            Screenshot.request_uuid == request_uuid, Screenshot.host_id == host_id,
            Screenshot.status == "requested",
        ))
        retention = (await get_value(db, "retention"))["screenshot_days"]
    if shot_id is None:
        raise ScreenshotRejected("Permintaan screenshot tidak ditemukan atau sudah selesai.")

    # Tulis file di luar transaksi; nama acak (bukan request_uuid) agar path tidak bisa ditebak dari log
    relative = Path(str(host_id)) / f"{uuid.uuid4().hex}{ext}"
    target = settings.SCREENSHOT_DIR / relative
    await run_in_threadpool(_write_file, target, content)

    async with db.begin():
        result = await db.execute(
            update(Screenshot).where(Screenshot.id == shot_id, Screenshot.status == "requested").values(
                status="uploaded", file_path=str(relative), size_bytes=len(content),
                width=width if width and 0 < width < 65536 else None,
                height=height if height and 0 < height < 65536 else None,
                expires_at=db_now_plus(retention * 86400),
            )
        )
        if result.rowcount == 0:                               # diunggah ganda bersamaan
            target.unlink(missing_ok=True)
            raise ScreenshotRejected("Permintaan screenshot sudah selesai.")


async def fail_screenshot(db: AsyncSession, host_id: int, request_uuid: str, error: str) -> bool:
    async with db.begin():
        result = await db.execute(
            update(Screenshot).where(
                Screenshot.request_uuid == request_uuid, Screenshot.host_id == host_id,
                Screenshot.status == "requested",
            ).values(status="failed", error=error[:200])
        )
    return result.rowcount > 0
