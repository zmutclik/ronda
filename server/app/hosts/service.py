import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.models import User
from app.core.database import db_now_plus
from app.folders.models import Folder
from app.folders.service import FolderTree
from app.hosts.models import (
    Host, HostDisk, HostEvent, HostLatest, InstalledApp, NetworkAdapter, RemoteSession, Screenshot,
)
from app.settings.service import get_value

PAGE_SIZE = 50
APPROVED = ("online", "offline")          # PC yang sudah disetujui (tampil di daftar Komputer)
PROBLEM_LEVELS = ("critical", "warning")
# remote_sessions.end_reason → teks (tab Remote, halaman remote)
REMOTE_END_REASONS = {
    "stopped": "dihentikan", "browser_closed": "jendela ditutup", "agent_closed": "koneksi PC terputus",
    "timeout": "agent tidak tersambung", "max_duration": "batas durasi", "failed": "gagal",
    "relay_restart": "relay dijalankan ulang", "expired": "kedaluwarsa",
}


async def offline_after(db: AsyncSession) -> int:
    return int(await get_value(db, "offline_after_sec"))


def live_status(threshold: int):
    """Status online dihitung dari last_seen_at saat dibaca, bukan menunggu job offline (jalan tiap menit)."""
    return case(
        (Host.status.not_in(APPROVED), Host.status),
        (Host.last_seen_at > db_now_plus(-threshold), "online"),
        else_="offline",
    ).label("live_status")


@dataclass
class HostRow:
    id: int
    hostname: str
    display_name: str | None
    folder_id: int | None
    last_ip: str | None
    last_seen_at: datetime | None
    agent_version: str | None
    problem_level: str | None
    problem_text: str | None
    live_status: str
    logged_user: str | None
    cpu_pct: float | None = None
    ram_pct: float | None = None
    approved_at: datetime | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.hostname

    @property
    def online(self) -> bool:
        return self.live_status == "online"

    @property
    def dot(self) -> str:
        if not self.online:
            return "off"
        return {"critical": "crit", "warning": "warn"}.get(self.problem_level or "", "on")

    @property
    def condition(self) -> str:
        if self.problem_text:
            return self.problem_text
        return "Normal" if self.online else "Offline"


def _row_columns(threshold: int):
    return (
        Host.id, Host.hostname, Host.display_name, Host.folder_id, Host.last_ip, Host.last_seen_at,
        Host.agent_version, Host.problem_level, Host.problem_text, live_status(threshold),
        HostLatest.logged_user, HostLatest.cpu_pct, HostLatest.ram_pct, Host.approved_at,
    )


def _rows(result) -> list[HostRow]:
    return [HostRow(**row._mapping) for row in result]


@dataclass
class GroupSummary:
    unit_id: int | None          # None = belum dikelompokkan
    total: int = 0
    problems: int = 0
    offline: int = 0


async def group_summaries(db: AsyncSession, threshold: int) -> dict[int | None, GroupSummary]:
    unit = func.coalesce(Folder.parent_id, Folder.id)
    offline = Host.last_seen_at.is_(None) | (Host.last_seen_at <= db_now_plus(-threshold))
    rows = await db.execute(
        select(
            unit.label("unit_id"), func.count(),
            func.sum(case((Host.problem_level.in_(PROBLEM_LEVELS) & ~offline, 1), else_=0)),
            func.sum(case((offline, 1), else_=0)),
        )
        .select_from(Host).outerjoin(Folder, Folder.id == Host.folder_id)
        .where(Host.status.in_(APPROVED))
        .group_by(unit)
    )
    return {r[0]: GroupSummary(r[0], r[1], int(r[2] or 0), int(r[3] or 0)) for r in rows}


async def list_hosts(
    db: AsyncSession, threshold: int, *, folder_ids: list[int] | None = None, unassigned: bool = False,
    q: str | None = None, status: str | None = None, problems_only: bool = False,
    after: tuple[str, int] | None = None, limit: int = PAGE_SIZE,
) -> tuple[list[HostRow], tuple[str, int] | None]:
    """Keyset per (hostname, id). Kembalikan baris dan cursor halaman berikutnya (None bila habis)."""
    stmt = (
        select(*_row_columns(threshold)).outerjoin(HostLatest, HostLatest.host_id == Host.id)
        .where(Host.status.in_(APPROVED)).order_by(Host.hostname, Host.id).limit(limit + 1)
    )
    if folder_ids is not None:
        stmt = stmt.where(Host.folder_id.in_(folder_ids))
    if unassigned:
        stmt = stmt.where(Host.folder_id.is_(None))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Host.hostname.like(like), Host.display_name.like(like), Host.last_ip.like(like),
                              HostLatest.logged_user.like(like)))
    online = Host.last_seen_at > db_now_plus(-threshold)
    if status == "online":
        stmt = stmt.where(online)
    elif status == "offline":
        stmt = stmt.where(Host.last_seen_at.is_(None) | ~online)
    if problems_only:
        stmt = stmt.where(or_(Host.problem_level.in_(PROBLEM_LEVELS), Host.last_seen_at.is_(None), ~online))
    if after:
        stmt = stmt.where(or_(Host.hostname > after[0], and_(Host.hostname == after[0], Host.id > after[1])))
    rows = _rows(await db.execute(stmt))
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, ((rows[-1].hostname, rows[-1].id) if more else None)


async def problem_hosts(db: AsyncSession, threshold: int, limit: int = 10) -> list[HostRow]:
    """Dashboard: PC dengan alert kritis/peringatan, kritis dulu."""
    stmt = (
        select(*_row_columns(threshold)).outerjoin(HostLatest, HostLatest.host_id == Host.id)
        .where(Host.status.in_(APPROVED), Host.problem_level.in_(PROBLEM_LEVELS))
        .order_by(func.field(Host.problem_level, *PROBLEM_LEVELS), Host.hostname).limit(limit)
    )
    return _rows(await db.execute(stmt))


async def status_counts(db: AsyncSession, threshold: int) -> dict[str, int]:
    online = Host.last_seen_at > db_now_plus(-threshold)
    approved = Host.status.in_(APPROVED)
    row = (await db.execute(select(
        func.sum(case((approved, 1), else_=0)),
        func.sum(case((approved & online, 1), else_=0)),
        func.sum(case((approved & Host.problem_level.in_(PROBLEM_LEVELS), 1), else_=0)),
        func.sum(case((Host.status == "pending", 1), else_=0)),
    ))).one()
    total, on, problems, pending = (int(v or 0) for v in row)
    return {"total": total, "online": on, "offline": total - on, "problems": problems, "pending": pending}


async def version_counts(db: AsyncSession) -> list[tuple[str, int]]:
    rows = await db.execute(
        select(Host.agent_version, func.count()).where(Host.status.in_(APPROVED))
        .group_by(Host.agent_version).order_by(func.count().desc())
    )
    return [(v or "?", n) for v, n in rows]


async def update_status_counts(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(
        select(Host.update_status, func.count()).where(Host.status.in_(APPROVED)).group_by(Host.update_status))
    return dict(rows.all())


# ---------- Detail ----------

@dataclass
class HostDetail:
    host: Host
    latest: HostLatest | None
    disks: list[HostDisk]
    live_status: str
    app_count: int
    nic_count: int
    approved_by: str | None

    @property
    def online(self) -> bool:
        return self.live_status == "online"


async def get_detail(db: AsyncSession, host_id: int, threshold: int) -> HostDetail | None:
    row = (await db.execute(
        select(Host, HostLatest, live_status(threshold), User.username)
        .outerjoin(HostLatest, HostLatest.host_id == Host.id)
        .outerjoin(User, User.id == Host.approved_by)
        .where(Host.id == host_id)
    )).one_or_none()
    if row is None:
        return None
    disks = list(await db.scalars(select(HostDisk).where(HostDisk.host_id == host_id).order_by(HostDisk.mount)))
    app_count = await db.scalar(select(func.count()).where(InstalledApp.host_id == host_id))
    nic_count = await db.scalar(select(func.count()).where(NetworkAdapter.host_id == host_id))
    return HostDetail(row[0], row[1], disks, row[2], app_count, nic_count, row[3])


async def neighbors(db: AsyncSession, host: Host) -> tuple[tuple[int, str] | None, tuple[int, str] | None, int, int]:
    """PC sebelum/sesudah dalam folder yang sama (urut hostname), posisi, dan jumlah PC di folder."""
    same = and_(Host.status.in_(APPROVED),
                Host.folder_id == host.folder_id if host.folder_id else Host.folder_id.is_(None))
    prev = (await db.execute(select(Host.id, Host.hostname).where(
        same, or_(Host.hostname < host.hostname, and_(Host.hostname == host.hostname, Host.id < host.id)))
        .order_by(Host.hostname.desc(), Host.id.desc()).limit(1))).first()
    nxt = (await db.execute(select(Host.id, Host.hostname).where(
        same, or_(Host.hostname > host.hostname, and_(Host.hostname == host.hostname, Host.id > host.id)))
        .order_by(Host.hostname, Host.id).limit(1))).first()
    total = await db.scalar(select(func.count()).select_from(Host).where(same))
    position = await db.scalar(select(func.count()).select_from(Host).where(
        same, or_(Host.hostname < host.hostname, and_(Host.hostname == host.hostname, Host.id <= host.id))))
    return (tuple(prev) if prev else None), (tuple(nxt) if nxt else None), position, total


async def events(db: AsyncSession, host_id: int, *, before_id: int | None = None, limit: int = PAGE_SIZE):
    stmt = select(HostEvent).where(HostEvent.host_id == host_id).order_by(HostEvent.id.desc()).limit(limit + 1)
    if before_id:
        stmt = stmt.where(HostEvent.id < before_id)
    rows = list(await db.scalars(stmt))
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (rows[-1].id if more else None)


async def apps(db: AsyncSession, host_id: int, *, q: str | None = None, limit: int | None = 20):
    stmt = select(InstalledApp).where(InstalledApp.host_id == host_id).order_by(InstalledApp.name)
    if q:
        stmt = stmt.where(or_(InstalledApp.name.like(f"%{q}%"), InstalledApp.publisher.like(f"%{q}%")))
    if limit:
        stmt = stmt.limit(limit)
    return list(await db.scalars(stmt))


async def adapters(db: AsyncSession, host_id: int) -> list[NetworkAdapter]:
    return list(await db.scalars(
        select(NetworkAdapter).where(NetworkAdapter.host_id == host_id)
        .order_by(NetworkAdapter.is_virtual, NetworkAdapter.is_server_route.desc(), NetworkAdapter.is_up.desc(),
                  NetworkAdapter.name)
    ))


async def screenshots(db: AsyncSession, host_id: int, limit: int = 10):
    return (await db.execute(
        select(Screenshot, User.username).join(User, User.id == Screenshot.requested_by)
        .where(Screenshot.host_id == host_id).order_by(Screenshot.id.desc()).limit(limit)
    )).all()


async def get_screenshot(db: AsyncSession, host_id: int, shot_id: int) -> Screenshot | None:
    return await db.scalar(select(Screenshot).where(Screenshot.id == shot_id, Screenshot.host_id == host_id))


async def remote_sessions(db: AsyncSession, host_id: int, limit: int = 10):
    return (await db.execute(
        select(RemoteSession, User.username).join(User, User.id == RemoteSession.user_id)
        .where(RemoteSession.host_id == host_id).order_by(RemoteSession.id.desc()).limit(limit)
    )).all()


# ---------- Aksi ----------

class HostActionError(ValueError):
    pass


async def move_hosts(
    db: AsyncSession, host_ids: list[int], folder_id: int | None, tree: FolderTree, actor: audit.Actor,
) -> int:
    if folder_id is not None and folder_id not in tree.by_id:
        raise HostActionError("Folder tujuan tidak ditemukan.")
    target = tree.path(folder_id)
    async with db.begin():
        hosts = list(await db.scalars(
            select(Host).where(Host.id.in_(host_ids), Host.status.in_(APPROVED)).with_for_update()))
        moved = [h for h in hosts if h.folder_id != folder_id]
        for host in moved:
            source = tree.path(host.folder_id)
            host.folder_id = folder_id
            db.add(HostEvent(host_id=host.id, kind="folder", message=f"Dipindah ke folder {target} oleh "
                             f"{actor.username}", meta={"from": source, "to": target}))
            await audit.record(db, actor, "pc", "Pindah folder", target_type="host", target_id=host.id,
                               target_label=host.hostname, detail=f"{source} → {target}")
    return len(moved)


# Field yang bisa diubah di tab Aksi: nama → (label, panjang maksimal)
EDITABLE_SPECS = {"hostname": ("Hostname", 63), "manufacturer": ("Merek", 60), "model": ("Model", 80),
                  "serial_number": ("Serial", 60), "inventory_no": ("Inventaris RS", 40)}
AGENT_SPECS = ("manufacturer", "model", "serial_number")     # juga dikirim agent lewat inventaris
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


async def update_specs(db: AsyncSession, host_id: int, values: dict[str, str | None], actor: audit.Actor) -> int:
    """Ubah data identitas PC. Merek/model/serial yang diisi manual dikunci dari data inventaris agent;
    dikosongkan = kembali memakai data dari agent pada inventaris berikutnya. Kembalikan jumlah field berubah."""
    clean: dict[str, str | None] = {}
    for field, (label, max_len) in EDITABLE_SPECS.items():
        value = (values.get(field) or "").strip() or None
        if value and len(value) > max_len:
            raise HostActionError(f"{label} maksimal {max_len} karakter.")
        clean[field] = value
    if not clean["hostname"]:
        raise HostActionError("Hostname wajib diisi.")
    if not HOSTNAME_RE.match(clean["hostname"]):
        raise HostActionError("Hostname hanya boleh huruf, angka, titik, minus, dan garis bawah.")
    async with db.begin():
        host = await db.get(Host, host_id, with_for_update=True)
        if host is None or host.status not in APPROVED:
            raise HostActionError("PC tidak ditemukan.")
        changes = {f: (getattr(host, f), v) for f, v in clean.items() if getattr(host, f) != v}
        if not changes:
            return 0
        label = host.hostname
        manual = set(host.manual_specs or ())
        for field, (_, new) in changes.items():
            setattr(host, field, new)
            if field in AGENT_SPECS:
                (manual.add if new else manual.discard)(field)
        host.manual_specs = sorted(manual) or None
        detail = " · ".join(f"{EDITABLE_SPECS[f][0]}: {old or '—'} → {new or '—'}" for f, (old, new) in changes.items())
        db.add(HostEvent(host_id=host_id, kind="inventory", message=f"Data PC diubah oleh {actor.username}"[:255],
                         meta={f: {"from": old, "to": new} for f, (old, new) in changes.items()}))
        await audit.record(db, actor, "pc", "Ubah data PC", target_type="host", target_id=host_id,
                           target_label=label, detail=detail)
    return len(changes)


async def request_inventory(db: AsyncSession, host_id: int, actor: audit.Actor) -> None:
    async with db.begin():
        host = await db.get(Host, host_id, with_for_update=True)
        if host is None or host.status not in APPROVED:
            raise HostActionError("PC tidak ditemukan.")
        host.inventory_requested_at = func.now()
        await audit.record(db, actor, "pc", "Refresh inventaris", target_type="host", target_id=host_id,
                           target_label=host.hostname)


async def request_screenshot(
    db: AsyncSession, host_id: int, *, reason: str, notify_user: bool, actor: audit.Actor, threshold: int,
) -> Screenshot:
    if not 3 <= len(reason) <= 200:
        raise HostActionError("Alasan wajib diisi (3–200 karakter).")
    async with db.begin():
        row = (await db.execute(select(Host, live_status(threshold)).where(Host.id == host_id)
                                .with_for_update())).one_or_none()
        if row is None or row[0].status not in APPROVED:
            raise HostActionError("PC tidak ditemukan.")
        host, status = row
        if status != "online":
            raise HostActionError("PC sedang offline; screenshot hanya bisa diambil dari PC yang online.")
        shot = Screenshot(request_uuid=str(uuid.uuid4()), host_id=host_id, requested_by=actor.user_id,
                          reason=reason, notify_user=notify_user)
        db.add(shot)
        db.add(HostEvent(host_id=host_id, kind="screenshot", level="warn",
                         message=f"Screenshot oleh {actor.username} · {reason}"[:255], meta={"ip": actor.ip}))
        await audit.record(db, actor, "screenshot", "Ambil screenshot", sensitive=True, target_type="host",
                           target_id=host_id, target_label=host.hostname,
                           detail=f"Alasan: {reason} · notifikasi ke user: {'ya' if notify_user else 'tidak'}")
        await db.flush()
        await db.refresh(shot)
    return shot


async def record_screenshot_view(db: AsyncSession, host: Host, shot: Screenshot, actor: audit.Actor) -> None:
    async with db.begin():
        await audit.record(db, actor, "screenshot", "Lihat screenshot", sensitive=True, target_type="host",
                           target_id=host.id, target_label=host.hostname, detail=f"Permintaan #{shot.id}")


async def revoke_token(db: AsyncSession, host_id: int, actor: audit.Actor) -> None:
    """Agent tidak bisa lagi mengirim data; PC harus mendaftar ulang dan disetujui lagi."""
    async with db.begin():
        host = await db.get(Host, host_id, with_for_update=True)
        if host is None:
            raise HostActionError("PC tidak ditemukan.")
        host.agent_token_hash = None
        host.status = "blocked"
        db.add(HostEvent(host_id=host_id, kind="agent", level="crit",
                         message=f"Token agent dicabut oleh {actor.username}"))
        await audit.record(db, actor, "agent", "Cabut token agent", sensitive=True, target_type="host",
                           target_id=host_id, target_label=host.hostname)


async def mark_screenshots_failed_without_pickup(db: AsyncSession, window: int) -> int:
    async with db.begin():
        result = await db.execute(
            update(Screenshot).where(Screenshot.status == "requested",
                                     Screenshot.requested_at < db_now_plus(-window))
            .values(status="failed", error="Agent tidak merespons (helper tidak aktif atau PC offline)")
        )
    return result.rowcount


async def folder_counts(db: AsyncSession) -> dict[int, int]:
    """Jumlah PC per folder (langsung, tanpa ruangan di bawahnya)."""
    rows = await db.execute(select(Host.folder_id, func.count()).where(
        Host.status.in_(APPROVED), Host.folder_id.is_not(None)).group_by(Host.folder_id))
    return dict(rows.all())
