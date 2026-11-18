import hashlib
import ipaddress
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.agents.models import INSTALLER_PLATFORMS, AgentInstaller, AgentRelease, EnrollmentKey
from app.auth import audit
from app.auth.service import hash_token
from app.core.config import settings
from app.core.database import db_now_plus
from app.folders.service import FolderTree, suggest
from app.hosts.ingest import ROLLBACK_MESSAGE
from app.hosts.models import Host, HostEvent
from app.hosts.schemas import EnrollIn
from app.hosts.service import APPROVED, visible
from app.settings.service import get_value

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # tanpa 0/O/1/I agar mudah dibaca dari layar
KEY_PREFIX_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,23}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

logger = logging.getLogger(__name__)


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
        # IP koneksi, BUKAN data.ip: field itu diisi agent sendiri, jadi pemegang key bisa mengaku berada di subnet
        if subnet and _in_subnet(ip, subnet):
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


def _save_release_file(source, directory: Path, max_bytes: int) -> tuple[Path, str, int]:
    """Salin upload ke file sementara bernama acak di `directory` sambil menghitung SHA-256 (thread pool: I/O + hash
    CPU). Nama acak: dua unggahan versi sama yang bersamaan tidak menulis ke file yang sama. Pemanggil memindahkan
    file ke nama akhirnya setelah baris DB berhasil dibuat, atau menghapusnya."""
    directory.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    tmp = directory / f".upload-{secrets.token_hex(8)}.part"
    try:
        with tmp.open("wb") as out:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise AgentError(f"Paket melebihi {max_bytes // (1024 * 1024)} MB.")
                digest.update(chunk)
                out.write(chunk)
    except OSError as err:         # mis. disk penuh: admin perlu tahu sebabnya, bukan "Terjadi kesalahan"
        tmp.unlink(missing_ok=True)
        raise AgentError(f"Paket gagal disimpan di server: {err.strerror or err}.") from err
    except AgentError:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, digest.hexdigest(), size


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
    async with db.begin():         # gagal cepat sebelum mengunggah ratusan MB; penentu sebenarnya UNIQUE di bawah
        if await db.scalar(select(AgentRelease.id).where(AgentRelease.version == version)):
            raise AgentError(f"Versi {version} sudah ada.")
    tmp, sha256, size = await run_in_threadpool(
        _save_release_file, upload, settings.RELEASE_DIR, settings.RELEASE_MAX_BYTES)
    relative = Path(f"ronda-agent-{version}.zip")
    try:
        async with db.begin():
            release = AgentRelease(version=version, sha256=sha256, signature=signature, file_path=str(relative),
                                   size_bytes=size, notes=notes or None, uploaded_by=actor.user_id)
            db.add(release)
            try:
                await db.flush()
            except IntegrityError as err:          # unggahan versi sama yang bersamaan menang lebih dulu
                raise AgentError(f"Versi {version} sudah ada.") from err
            # File akhir baru ditulis setelah baris versi ini milik kita: unggahan lain tidak bisa menimpanya.
            # Commit gagal setelah ini = file tanpa baris DB, yang akan ditimpa unggahan berikutnya versi itu.
            tmp.replace(settings.RELEASE_DIR / relative)
            await db.refresh(release)
            await audit.record(db, actor, "agent", "Unggah rilis", target_type="release", target_id=release.id,
                               target_label=f"v{version}", detail=f"SHA-256 {sha256[:16]}…")
    finally:
        tmp.unlink(missing_ok=True)
    return release


async def change_release(db: AsyncSession, release_id: int, action: str, actor: audit.Actor, *,
                         note: str | None = None, expect_ring: int | None = None) -> AgentRelease:
    """start (Ring 0) | promote (naik satu ring) | pause | resume | stop. `note` ditambahkan ke detail audit;
    `expect_ring` (dipakai auto_promote): batal bila ring sudah berubah sejak dinilai."""
    async with db.begin():
        release = await db.get(AgentRelease, release_id, with_for_update=True, populate_existing=True)
        if release is None:
            raise AgentError("Rilis tidak ditemukan.")
        if expect_ring is not None and release.current_ring != expect_ring:
            raise AgentError("Ring rilis sudah berubah.")
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
            if action == "resume":
                # Waktu tunggu naik ring otomatis dihitung ulang, dan rollback sebelum dilanjutkan sudah dilihat admin
                # (tanpa ini auto_promote langsung menjeda lagi karena rollback yang sama).
                release.ring_started_at = func.now()
            detail = {"pause": "Rilis dijeda", "resume": "Rilis dilanjutkan", "stop": "Rilis dihentikan"}[action]
        else:
            raise AgentError("Aksi tidak dikenal.")
        label = {"start": "Mulai rilis", "promote": "Naikkan rilis", "pause": "Jeda rilis",
                 "resume": "Lanjutkan rilis", "stop": "Hentikan rilis"}[action]
        if note:
            detail = f"{detail} (otomatis: {note})"
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


async def ring_stats(db: AsyncSession, release: AgentRelease, *, hidden: frozenset[int]) -> list[RingStats]:
    installed = Host.agent_version == release.version
    rows = (await db.execute(
        select(
            Host.ring, func.count(),
            func.sum(case((installed, 1), else_=0)),
            func.sum(case((~installed & (Host.update_status == "downloading"), 1), else_=0)),
            func.sum(case((~installed & (Host.update_status == "waiting"), 1), else_=0)),
            func.sum(case((Host.update_status.in_(("rollback", "failed")), 1), else_=0)),
        ).where(Host.status.in_(APPROVED), visible(hidden)).group_by(Host.ring).order_by(Host.ring)
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



# ---------- Rilis dari repo (git pull) dan naik ring otomatis ----------
# Mesin build menaruh zip bertanda tangan + metadata JSON di agent/releases/ lalu commit. Setelah `git pull` di server,
# worker (folder itu di-mount read-only ke RELEASE_REPO_DIR) mendaftarkan versi baru, memulai versi terbaru di Ring 0,
# dan menaikkan ring setelah RELEASE_RING_HOLD_HOURS bila tidak ada rollback. Agent tetap memverifikasi Ed25519,
# jadi isi repo tidak perlu dipercaya lebih dari form unggah.

_bad_repo_files: dict[Path, tuple[float, int]] = {}    # metadata yang sudah ditolak (mtime, size): tidak dicek ulang


def read_repo_release(meta_path: Path) -> dict:
    """Validasi ronda-agent-<versi>.json; kembalikan dict berisi version, sha256, size, signature, notes, zip."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise AgentError(f"{meta_path.name}: JSON tidak terbaca ({err})") from err
    if not isinstance(meta, dict):
        raise AgentError(f"{meta_path.name}: bukan objek JSON")
    version = str(meta.get("version", ""))
    if not VERSION_PATTERN.match(version) or len(version) > 20:
        raise AgentError(f"{meta_path.name}: versi tidak valid")
    if meta_path.name != f"ronda-agent-{version}.json":
        raise AgentError(f"{meta_path.name}: nama file harus ronda-agent-{version}.json")
    sha256, signature, size = meta.get("sha256"), meta.get("signature"), meta.get("size")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise AgentError(f"{meta_path.name}: sha256 tidak valid")
    if not isinstance(signature, str) or not 40 <= len(signature) <= 128:
        raise AgentError(f"{meta_path.name}: signature tidak valid")
    if not isinstance(size, int) or not 0 < size <= settings.RELEASE_MAX_BYTES:
        raise AgentError(f"{meta_path.name}: size tidak valid")
    notes = meta.get("notes") or None
    if notes is not None and (not isinstance(notes, str) or len(notes) > 500):
        raise AgentError(f"{meta_path.name}: notes maksimal 500 karakter")
    return {"version": version, "sha256": sha256, "signature": signature, "size": size, "notes": notes,
            "zip": meta_path.with_name(f"ronda-agent-{version}.zip")}


def _copy_repo_zip(meta: dict) -> Path:
    """Salin zip ke RELEASE_DIR (file sementara) sambil mencocokkan ukuran + SHA-256 dengan metadata."""
    try:
        with meta["zip"].open("rb") as source:
            tmp, sha256, size = _save_release_file(source, settings.RELEASE_DIR, settings.RELEASE_MAX_BYTES)
    except FileNotFoundError as err:
        raise AgentError(f"{meta['zip'].name} tidak ada") from err
    if (sha256, size) != (meta["sha256"], meta["size"]):
        tmp.unlink(missing_ok=True)
        raise AgentError(f"{meta['zip'].name}: ukuran/SHA-256 tidak cocok dengan metadata")
    return tmp


async def sync_repo_releases(db: AsyncSession, directory: Path) -> list[str]:
    """Daftarkan versi di `directory` yang belum ada di DB; versi terbaru langsung dimulai di Ring 0 bila lebih baru
    dari semua rilis yang ada. Versi yang sudah terdaftar (termasuk yang dihentikan admin) tidak disentuh lagi.
    Kembalikan versi yang baru didaftarkan."""
    if not directory.is_dir():
        return []
    async with db.begin():
        known = set(await db.scalars(select(AgentRelease.version)))
    added: list[str] = []
    for meta_path in sorted(directory.glob("ronda-agent-*.json")):
        stat = meta_path.stat()
        if _bad_repo_files.get(meta_path) == (stat.st_mtime, stat.st_size):
            continue
        try:
            meta = read_repo_release(meta_path)
            if meta["version"] in known:
                continue
            tmp = await run_in_threadpool(_copy_repo_zip, meta)
        except AgentError as err:
            _bad_repo_files[meta_path] = (stat.st_mtime, stat.st_size)
            logger.error("repo_release_rejected", extra={"file": meta_path.name, "error": str(err)})
            continue
        version = meta["version"]
        relative = Path(f"ronda-agent-{version}.zip")
        try:
            async with db.begin():
                release = AgentRelease(version=version, sha256=meta["sha256"], signature=meta["signature"],
                                       file_path=str(relative), size_bytes=meta["size"], notes=meta["notes"],
                                       uploaded_by=None)
                db.add(release)
                try:
                    await db.flush()
                except IntegrityError:          # diunggah lewat form pada saat yang sama
                    continue
                tmp.replace(settings.RELEASE_DIR / relative)
                await audit.record(db, audit.SYSTEM, "agent", "Rilis dari repo", target_type="release",
                                   target_id=release.id, target_label=f"v{version}",
                                   detail=f"SHA-256 {meta['sha256'][:16]}…")
        finally:
            tmp.unlink(missing_ok=True)
        known.add(version)
        added.append(version)
        logger.info("repo_release_added", extra={"version": version})
    if added:
        newest = max(added, key=_version_key)
        async with db.begin():
            others = [v for v in await db.scalars(select(AgentRelease.version).where(AgentRelease.version != newest))]
            release_id = await db.scalar(select(AgentRelease.id).where(AgentRelease.version == newest))
        if all(_version_key(v) < _version_key(newest) for v in others):
            await change_release(db, release_id, "start", audit.SYSTEM)
    return added


def ring_hold(ring: int) -> timedelta:
    return timedelta(hours=(settings.RELEASE_RING0_HOURS, settings.RELEASE_RING1_HOURS)[ring])


@dataclass
class PromoteCheck:
    """Hasil penilaian naik ring otomatis: `action` none | wait | promote | pause; `reason` untuk UI/audit (kosong =
    tidak ada penghalang, tinggal menunggu `due_at`)."""
    action: str
    reason: str
    due_at: datetime | None = None


async def promote_check(db: AsyncSession, release: AgentRelease) -> PromoteCheck:
    """Rilis rolling di Ring 0/1 naik satu ring setelah ring_hold bila sejak ring_started_at tidak ada PC (Ring ≤
    ring rilis) yang rollback, tidak ada PC online yang gagal unduh/verifikasi, dan (bila ada PC online di cakupan)
    minimal satu sudah terpasang versi ini. Ada rollback → rilis dijeda; admin memeriksa lalu Lanjutkan."""
    if not settings.RELEASE_AUTO_PROMOTE or release.status != "rolling" or release.current_ring >= 2:
        return PromoteCheck("none", "")
    ring, started = release.current_ring, release.ring_started_at
    in_scope = (Host.ring <= ring) & Host.status.in_(APPROVED)
    # `>`: rollback di detik yang sama dengan Lanjutkan (ring_started_at) dianggap sudah dilihat admin
    rollbacks = await db.scalar(
        select(func.count(func.distinct(HostEvent.host_id))).select_from(Host)
        .join(HostEvent, HostEvent.host_id == Host.id)
        .where(in_scope, HostEvent.kind == "agent", HostEvent.message == ROLLBACK_MESSAGE, HostEvent.ts > started))
    if rollbacks:
        return PromoteCheck("pause", f"{rollbacks} PC rollback ke versi sebelumnya")
    installed = Host.agent_version == release.version
    online, done, failed = (await db.execute(select(
        func.count(), func.sum(case((installed, 1), else_=0)),
        func.sum(case((~installed & (Host.update_status == "failed"), 1), else_=0)),
    ).where(in_scope, Host.status == "online"))).one()
    due_at = started + ring_hold(ring)
    if failed:
        return PromoteCheck("wait", f"{failed} PC gagal mengunduh/memverifikasi paket", due_at)
    if online and not done:
        return PromoteCheck("wait", "belum ada PC yang terpasang versi ini", due_at)
    if await db.scalar(select(func.now())) < due_at:
        return PromoteCheck("wait", "", due_at)             # tinggal menunggu waktu
    return PromoteCheck("promote", f"stabil {ring_hold(ring).total_seconds() / 3600:g} jam di Ring {ring}")


async def auto_promote(db: AsyncSession) -> PromoteCheck | None:
    """Dipanggil worker tiap ±60 detik: jeda atau naikkan rilis yang sedang berjalan sesuai promote_check."""
    async with db.begin():
        release = await db.scalar(select(AgentRelease).where(AgentRelease.status == "rolling"))
        if release is None:
            return None
        check = await promote_check(db, release)
    if check.action not in ("pause", "promote"):
        return check
    try:
        await change_release(db, release.id, check.action, audit.SYSTEM, note=check.reason,
                             expect_ring=release.current_ring)
    except AgentError:                 # admin menjeda/menaikkan/menghentikan di antara penilaian dan perubahan
        return None
    logger.info("release_auto_" + check.action, extra={"version": release.version, "reason": check.reason})
    return check

# ---------- Installer (pemasangan lewat skrip) ----------

INSTALLER_FILES = {   # platform → (nama file, magic bytes awal file)
    "windows": ("ronda-agent-setup-{version}.exe", b"MZ"),
    "linux": ("ronda-agent-linux-x86_64-{version}.tar.gz", b"\x1f\x8b"),
}
INSTALLER_SUBDIR = "installers"


async def upload_installer(db: AsyncSession, *, platform: str, version: str, upload, actor: audit.Actor
                           ) -> AgentInstaller:
    """Installer TIDAK diverifikasi tanda tangan Ed25519 seperti paket rilis: siapa pun yang memasang lewat
    /install.ps1 atau /install.sh menjalankan file ini sebagai Administrator/root. Karena itu hanya untuk hak
    agent_release dan dicatat sensitif di Audit Log; skrip memverifikasi SHA-256 unduhan terhadap baris ini."""
    if platform not in INSTALLER_PLATFORMS:
        raise AgentError("Platform harus Windows atau Linux.")
    if not VERSION_PATTERN.match(version) or len(version) > 20:
        raise AgentError("Versi harus format x.y.z, mis. 1.3.0.")
    async with db.begin():
        if await db.scalar(select(AgentInstaller.id).where(AgentInstaller.platform == platform,
                                                            AgentInstaller.version == version)):
            raise AgentError(f"Installer {platform} versi {version} sudah ada.")
    directory = settings.RELEASE_DIR / INSTALLER_SUBDIR
    tmp, sha256, size = await run_in_threadpool(_save_release_file, upload, directory, settings.RELEASE_MAX_BYTES)
    name, magic = INSTALLER_FILES[platform]
    try:
        with tmp.open("rb") as f:
            if f.read(len(magic)) != magic:
                kind = "setup .exe (Inno Setup)" if platform == "windows" else ".tar.gz dari build-linux.sh"
                raise AgentError(f"File bukan installer {platform}: unggah {kind}.")
        relative = Path(INSTALLER_SUBDIR) / name.format(version=version)
        async with db.begin():
            installer = AgentInstaller(platform=platform, version=version, sha256=sha256, file_path=str(relative),
                                       size_bytes=size, uploaded_by=actor.user_id)
            db.add(installer)
            try:
                await db.flush()
            except IntegrityError as err:
                raise AgentError(f"Installer {platform} versi {version} sudah ada.") from err
            tmp.replace(settings.RELEASE_DIR / relative)
            await db.refresh(installer)
            await audit.record(db, actor, "agent", "Unggah installer", sensitive=True, target_type="installer",
                               target_id=installer.id, target_label=f"{platform} v{version}",
                               detail=f"SHA-256 {sha256[:16]}…")
    finally:
        tmp.unlink(missing_ok=True)
    return installer


async def latest_installers(db: AsyncSession) -> dict[str, AgentInstaller]:
    """Installer terbaru (unggahan terakhir) per platform; platform tanpa installer tidak ada di dict."""
    latest = select(func.max(AgentInstaller.id)).group_by(AgentInstaller.platform)
    rows = await db.scalars(select(AgentInstaller).where(AgentInstaller.id.in_(latest)))
    return {row.platform: row for row in rows}


async def installers(db: AsyncSession, limit: int = 10) -> list[AgentInstaller]:
    return list(await db.scalars(select(AgentInstaller).order_by(AgentInstaller.id.desc()).limit(limit)))


async def get_installer(db: AsyncSession, installer_id: int) -> AgentInstaller | None:
    return await db.get(AgentInstaller, installer_id)
