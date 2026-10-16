import hashlib
import ipaddress
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.agents.models import AgentRelease, EnrollmentKey
from app.auth import audit
from app.auth.service import hash_token
from app.core.config import settings
from app.core.database import db_now_plus
from app.folders.service import FolderTree, suggest
from app.hosts.models import Host, HostEvent
from app.hosts.schemas import EnrollIn
from app.hosts.service import APPROVED
from app.settings.service import get_value

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # tanpa 0/O/1/I agar mudah dibaca dari layar
KEY_PREFIX_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,23}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class AgentError(ValueError):
    pass


# ---------- Enrollment key ----------

def _random_block(n: int = 4) -> str:
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(n))


async def create_enrollment_key(
    db: AsyncSession, *, prefix: str, valid_days: int | None, actor: audit.Actor,
) -> tuple[EnrollmentKey, str]:
    """Key mentah dikembalikan sekali (untuk ditampilkan); DB hanya menyimpan hash-nya."""
    prefix = prefix.strip().upper()
    if not KEY_PREFIX_PATTERN.match(prefix):
        raise AgentError("Awalan key 2–24 karakter: huruf besar, angka, dan tanda hubung.")
    if valid_days is not None and not 1 <= valid_days <= 366:
        raise AgentError("Masa berlaku 1–366 hari.")
    raw = f"{prefix}-{_random_block()}-{_random_block()}"
    async with db.begin():
        key = EnrollmentKey(
            key_hash=hash_token(raw), key_prefix=prefix, created_by=actor.user_id,
            expires_at=db_now_plus(valid_days * 86400) if valid_days else None,
        )
        db.add(key)
        await db.flush()
        await db.refresh(key)
        await audit.record(db, actor, "agent", "Buat enrollment key", sensitive=True, target_type="key",
                           target_id=key.id, target_label=prefix,
                           detail=f"Berlaku {valid_days} hari" if valid_days else "Tanpa batas waktu")
    return key, raw


async def revoke_enrollment_key(db: AsyncSession, key_id: int, actor: audit.Actor) -> None:
    async with db.begin():
        key = await db.get(EnrollmentKey, key_id, with_for_update=True)
        if key is None or key.revoked_at is not None:
            raise AgentError("Key tidak ditemukan atau sudah dicabut.")
        key.revoked_at = func.now()
        await audit.record(db, actor, "agent", "Cabut enrollment key", sensitive=True, target_type="key",
                           target_id=key_id, target_label=key.key_prefix)


def _key_usable():
    return (EnrollmentKey.revoked_at.is_(None)
            & or_(EnrollmentKey.expires_at.is_(None), EnrollmentKey.expires_at > func.now()))


async def active_keys(db: AsyncSession) -> list[EnrollmentKey]:
    return list(await db.scalars(select(EnrollmentKey).where(_key_usable()).order_by(EnrollmentKey.id.desc())))


async def enroll(db: AsyncSession, data: EnrollIn, ip: str) -> tuple[int, str, str]:
    """Daftar PC baru (status pending). Kembalikan (host_id, token agent mentah, status)."""
    token = secrets.token_urlsafe(32)
    async with db.begin():
        key = await db.scalar(select(EnrollmentKey).where(
            EnrollmentKey.key_hash == hash_token(data.enrollment_key), _key_usable()).with_for_update())
        if key is None:
            raise AgentError("Enrollment key tidak valid atau sudah tidak berlaku.")
        key.use_count = EnrollmentKey.use_count + 1
        host = Host(hostname=data.hostname, agent_token_hash=hash_token(token), enrollment_key_id=key.id,
                    agent_version=data.agent_version, serial_number=data.serial_number,
                    last_ip=data.ip or ip, last_seen_at=func.now())
        db.add(host)
        await db.flush()
        db.add(HostEvent(host_id=host.id, kind="agent", message=f"Agent mendaftar dengan key {key.key_prefix}"))
        status = "pending"
        subnet = await get_value(db, "auto_approve_subnet")
        if subnet and _in_subnet(data.ip or ip, subnet):
            await _approve(db, host, None, audit.SYSTEM, note="otomatis (subnet)")
            status = "online"
    return host.id, token, status


def _in_subnet(ip: str, subnet: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return False


# ---------- Persetujuan PC baru ----------

@dataclass
class PendingHost:
    id: int
    hostname: str
    last_ip: str | None
    created_at: object
    suggestion_id: int | None


async def pending_hosts(db: AsyncSession, tree: FolderTree) -> list[PendingHost]:
    rows = (await db.execute(
        select(Host.id, Host.hostname, Host.last_ip, Host.created_at)
        .where(Host.status == "pending").order_by(Host.id).limit(200)
    )).all()
    out = []
    for r in rows:
        node = suggest(tree, r.last_ip)
        out.append(PendingHost(r.id, r.hostname, r.last_ip, r.created_at, node.id if node else None))
    return out


async def _approve(db: AsyncSession, host: Host, folder_id: int | None, actor: audit.Actor,
                   note: str | None = None, folder_path: str | None = None) -> None:
    host.status = "online"            # heartbeat sudah berjalan saat pending; job offline mengoreksi bila tidak
    host.folder_id = folder_id
    host.approved_by = actor.user_id
    host.approved_at = func.now()
    target = folder_path or "Belum dikelompokkan"
    db.add(HostEvent(host_id=host.id, kind="agent", level="ok",
                     message=f"Disetujui oleh {actor.username} · folder {target}"))
    await audit.record(db, actor, "pc", "Setujui PC baru", target_type="host", target_id=host.id,
                       target_label=host.hostname, detail=f"Masuk folder {target}" + (f" · {note}" if note else ""))


async def approve_hosts(
    db: AsyncSession, choices: dict[int, int | None], tree: FolderTree, actor: audit.Actor,
) -> int:
    """choices: host_id → folder_id (None = belum dikelompokkan)."""
    for folder_id in choices.values():
        if folder_id is not None and folder_id not in tree.by_id:
            raise AgentError("Folder tujuan tidak ditemukan.")
    async with db.begin():
        hosts = list(await db.scalars(select(Host).where(
            Host.id.in_(list(choices)), Host.status == "pending").with_for_update()))
        for host in hosts:
            folder_id = choices[host.id]
            await _approve(db, host, folder_id, actor, folder_path=tree.path(folder_id) if folder_id else None)
    return len(hosts)


async def reject_host(db: AsyncSession, host_id: int, actor: audit.Actor) -> None:
    async with db.begin():
        host = await db.get(Host, host_id, with_for_update=True)
        if host is None or host.status != "pending":
            raise AgentError("PC tidak menunggu persetujuan.")
        host.status = "rejected"
        host.agent_token_hash = None
        await audit.record(db, actor, "pc", "Tolak PC baru", target_type="host", target_id=host_id,
                           target_label=host.hostname, detail=f"IP {host.last_ip or '—'}")


# ---------- Rilis agent ----------

def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _save_release_file(source, target: Path, max_bytes: int) -> tuple[str, int]:
    """Salin upload ke RELEASE_DIR sambil menghitung SHA-256 (thread pool: I/O + hash CPU)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    tmp = target.with_suffix(".part")
    with tmp.open("wb") as out:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                out.close()
                tmp.unlink(missing_ok=True)
                raise AgentError(f"Paket melebihi {max_bytes // (1024 * 1024)} MB.")
            digest.update(chunk)
            out.write(chunk)
    tmp.replace(target)
    return digest.hexdigest(), size


async def upload_release(
    db: AsyncSession, *, version: str, signature: str, notes: str | None, upload, actor: audit.Actor,
) -> AgentRelease:
    if not VERSION_PATTERN.match(version) or len(version) > 20:
        raise AgentError("Versi harus format x.y.z, mis. 1.3.0.")
    signature = signature.strip()
    if not 40 <= len(signature) <= 128:
        raise AgentError("Tanda tangan Ed25519 (base64) wajib diisi.")
    if notes and len(notes) > 500:
        raise AgentError("Catatan maksimal 500 karakter.")
    async with db.begin():
        if await db.scalar(select(AgentRelease.id).where(AgentRelease.version == version)):
            raise AgentError(f"Versi {version} sudah ada.")
    relative = Path(f"ronda-agent-{version}.zip")
    sha256, size = await run_in_threadpool(
        _save_release_file, upload, settings.RELEASE_DIR / relative, settings.RELEASE_MAX_BYTES)
    async with db.begin():
        release = AgentRelease(version=version, sha256=sha256, signature=signature, file_path=str(relative),
                               size_bytes=size, notes=notes or None, uploaded_by=actor.user_id)
        db.add(release)
        await db.flush()
        await db.refresh(release)
        await audit.record(db, actor, "agent", "Unggah rilis", target_type="release", target_id=release.id,
                           target_label=f"v{version}", detail=f"SHA-256 {sha256[:16]}…")
    return release


async def change_release(db: AsyncSession, release_id: int, action: str, actor: audit.Actor) -> AgentRelease:
    """start (Ring 0) | promote (naik satu ring) | pause | resume | stop."""
    async with db.begin():
        release = await db.get(AgentRelease, release_id, with_for_update=True)
        if release is None:
            raise AgentError("Rilis tidak ditemukan.")
        before = f"Ring {release.current_ring}"
        if action == "start":
            if release.status != "uploaded":
                raise AgentError("Rilis ini sudah pernah dimulai.")
            # Satu rilis berjalan pada satu waktu: rilis lain yang belum selesai dihentikan
            await db.execute(update(AgentRelease).where(
                AgentRelease.id != release_id, AgentRelease.status.in_(("rolling", "paused"))
            ).values(status="stopped"))
            release.status, release.current_ring, release.ring_started_at = "rolling", 0, func.now()
            detail = "Mulai di Ring 0"
        elif action == "promote":
            if release.status != "rolling" or release.current_ring >= 2:
                raise AgentError("Rilis tidak bisa dinaikkan.")
            release.current_ring += 1
            release.ring_started_at = func.now()
            detail = f"{before} → Ring {release.current_ring}"
        elif action in ("pause", "resume", "stop"):
            allowed = {"pause": ("rolling",), "resume": ("paused",), "stop": ("rolling", "paused")}[action]
            if release.status not in allowed:
                raise AgentError("Status rilis tidak sesuai untuk aksi ini.")
            release.status = {"pause": "paused", "resume": "rolling", "stop": "stopped"}[action]
            detail = {"pause": "Rilis dijeda", "resume": "Rilis dilanjutkan", "stop": "Rilis dihentikan"}[action]
        else:
            raise AgentError("Aksi tidak dikenal.")
        label = {"start": "Mulai rilis", "promote": "Naikkan rilis", "pause": "Jeda rilis",
                 "resume": "Lanjutkan rilis", "stop": "Hentikan rilis"}[action]
        await audit.record(db, actor, "agent", label, sensitive=action == "stop", target_type="release",
                           target_id=release_id, target_label=f"v{release.version}", detail=detail)
        await db.flush()
        await db.refresh(release)
    return release


async def releases(db: AsyncSession, limit: int = 10) -> list[AgentRelease]:
    return list(await db.scalars(select(AgentRelease).order_by(AgentRelease.id.desc()).limit(limit)))


async def current_release(db: AsyncSession) -> AgentRelease | None:
    return await db.scalar(select(AgentRelease).where(AgentRelease.status.in_(("rolling", "paused")))
                           .order_by(AgentRelease.id.desc()).limit(1))


@dataclass
class RingStats:
    ring: int
    total: int
    installed: int
    downloading: int
    waiting: int
    rollback: int


async def ring_stats(db: AsyncSession, release: AgentRelease) -> list[RingStats]:
    installed = Host.agent_version == release.version
    rows = (await db.execute(
        select(
            Host.ring, func.count(),
            func.sum(case((installed, 1), else_=0)),
            func.sum(case((~installed & (Host.update_status == "downloading"), 1), else_=0)),
            func.sum(case((~installed & (Host.update_status == "waiting"), 1), else_=0)),
            func.sum(case((Host.update_status.in_(("rollback", "failed")), 1), else_=0)),
        ).where(Host.status.in_(APPROVED)).group_by(Host.ring).order_by(Host.ring)
    )).all()
    return [RingStats(r[0], r[1], *(int(v or 0) for v in r[2:])) for r in rows]


async def update_for_host(db: AsyncSession, host_ring: int, host_version: str | None) -> AgentRelease | None:
    """Rilis yang harus dipasang host ini: status rolling, ring host sudah tercakup, dan versinya lebih baru."""
    release = await db.scalar(select(AgentRelease).where(
        AgentRelease.status == "rolling", AgentRelease.current_ring >= host_ring))
    if release is None:
        return None
    try:
        if host_version and _version_key(host_version) >= _version_key(release.version):
            return None
    except ValueError:
        pass                      # versi host tidak terbaca: tawarkan rilis
    return release


async def get_release_by_version(db: AsyncSession, version: str) -> AgentRelease | None:
    return await db.scalar(select(AgentRelease).where(AgentRelease.version == version))


async def get_release(db: AsyncSession, release_id: int) -> AgentRelease | None:
    return await db.get(AgentRelease, release_id)
