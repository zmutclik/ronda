import asyncio
import hashlib
import ipaddress
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import case, delete, false, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth import audit
from app.auth.models import FULL_NAME_MAX_LENGTH, ROLES, USERNAME_MAX_LENGTH, LoginFailure, User, UserSession
from app.core.config import settings
from app.core.database import db_now_plus

_hasher = PasswordHasher()
# Diverifikasi saat username tidak ada, agar waktu respons tidak membocorkan username valid
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
# Batasi argon2 paralel: tanpa ini lonjakan login memakai seluruh threadpool (40) × 64 MiB RAM per proses
_hash_slots = asyncio.Semaphore(settings.PASSWORD_HASH_CONCURRENCY)


# NIST SP 800-63B-4: minimal 15 karakter bila password satu-satunya faktor. Batas atas menahan argon2 dari
# teks raksasa (CPU/RAM per hash), tetap cukup longgar untuk kalimat sandi.
PASSWORD_MIN_LENGTH = 15
PASSWORD_MAX_LENGTH = 128


class TooManyAttempts(Exception):
    pass


class InvalidPassword(ValueError):
    pass


def validate_password(password: str) -> None:
    """Kebijakan password di service, bukan di form/CLI: setiap jalur yang mengganti password ikut terjaga."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise InvalidPassword(f"Password minimal {PASSWORD_MIN_LENGTH} karakter.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise InvalidPassword(f"Password maksimal {PASSWORD_MAX_LENGTH} karakter.")


def validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Role tidak dikenal: {role!r} (pilihan: {', '.join(ROLES)})")


def validate_identity(username: str, full_name: str) -> None:
    if not 0 < len(username) <= USERNAME_MAX_LENGTH:
        raise ValueError(f"Username harus 1–{USERNAME_MAX_LENGTH} karakter.")
    if not 0 < len(full_name) <= FULL_NAME_MAX_LENGTH:
        raise ValueError(f"Nama harus 1–{FULL_NAME_MAX_LENGTH} karakter.")


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def rate_limit_key(ip: str) -> str:
    """Kunci rate limit login per IP. IPv6 dipotong ke prefix /64: satu pelanggan biasanya memegang seluruh /64,
    jadi batas per alamat bisa ditembus dengan berganti alamat di dalam prefix yang sama."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:45]
    if addr.version == 6:
        if addr.ipv4_mapped:                             # ::ffff:1.2.3.4 = klien IPv4
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


async def _run_hasher(fn, *args):
    # argon2 CPU-bound (~50–300 ms): threadpool, bukan event loop; antre bila slot penuh
    async with _hash_slots:
        return await run_in_threadpool(fn, *args)


async def hash_password(password: str) -> str:
    return await _run_hasher(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > PASSWORD_MAX_LENGTH:
        password = ""                # tidak mungkin cocok; hash tetap dihitung agar waktu respons sama
    try:
        return await _run_hasher(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def create_user(
    db: AsyncSession, username: str, password: str, role: str, *, full_name: str | None = None,
    must_change_password: bool = False, actor: audit.Actor | None = None,
) -> User:
    full_name = full_name or username
    validate_identity(username, full_name)
    validate_role(role)
    validate_password(password)
    password_hash = await hash_password(password)       # di luar transaksi
    async with db.begin():
        user = User(username=username, full_name=full_name, password_hash=password_hash, role=role,
                    must_change_password=must_change_password)
        db.add(user)
        await db.flush()
        await db.refresh(user)
        if actor:
            await audit.record(db, actor, "user", "Tambah user", sensitive=True, target_type="user",
                               target_id=user.id, target_label=user.username, detail=f"Role: {role}")
    return user


async def login(db: AsyncSession, username: str, password: str, ip: str) -> str | None:
    """Kembalikan session ID mentah untuk cookie, atau None bila kredensial salah."""
    # Percobaan dicatat sebagai gagal SEBELUM dihitung (commit dulu). Percobaan paralel ke-k pasti melihat
    # ≥ k baris, sehingga paling banyak LOGIN_MAX_FAILURES yang lolos ke verifikasi. Hitung-dulu-catat-belakangan
    # bisa ditembus: semua request paralel membaca hitungan lama sebelum ada yang mencatat.
    # Batas per (user, IP), bukan per user: penyerang dari IP lain tidak bisa mengunci akun korban.
    # Batas per IP yang lebih longgar menahan tebakan banyak username dari satu IP.
    # Dicatat per user_id hasil lookup, bukan teks username: varian yang dianggap sama oleh collation
    # (Alice/alice) tetap satu hitungan, dan teks yang diketik (bisa saja password) tidak tersimpan.
    if len(username) > USERNAME_MAX_LENGTH:
        username = ""                                    # tidak mungkin cocok; hindari query dengan teks panjang
    raw_ip, ip = ip, rate_limit_key(ip)
    async with db.begin():
        user = await db.scalar(select(User).where(User.username == username))
        db.add(LoginFailure(user_id=user.id if user else None, ip=ip))
    async with db.begin():
        same_user = LoginFailure.user_id == user.id if user else false()
        per_ip, per_pair = (await db.execute(
            select(func.count(), func.sum(case((same_user, 1), else_=0))).where(
                LoginFailure.ip == ip,
                LoginFailure.created_at > db_now_plus(-settings.LOGIN_FAILURE_WINDOW),
            )
        )).one()
        blocked = per_pair > settings.LOGIN_MAX_FAILURES or per_ip > settings.LOGIN_MAX_FAILURES_PER_IP
        if blocked and user is not None and per_pair == settings.LOGIN_MAX_FAILURES + 1:
            # Satu catatan per lockout (bukan per percobaan): serangan tidak membanjiri Audit Log
            await audit.record(db, audit.Actor(user.id, user.username, raw_ip), "login",
                               f"Login gagal ×{settings.LOGIN_MAX_FAILURES}", sensitive=True,
                               detail=f"Dikunci {settings.LOGIN_FAILURE_WINDOW // 60} mnt untuk IP ini")
    if blocked:
        raise TooManyAttempts

    # Verifikasi hash di luar transaksi: ~100 ms CPU tidak menahan koneksi DB
    valid = await verify_password(user.password_hash if user else _DUMMY_HASH, password)
    if user is None or not valid or not user.is_active:
        return None                                      # baris percobaan tetap ada = tercatat gagal

    async with db.begin():
        await db.execute(delete(LoginFailure).where(LoginFailure.ip == ip, LoginFailure.user_id == user.id))
        await db.execute(update(User).where(User.id == user.id).values(last_login_at=func.now()))
        raw_sid = secrets.token_urlsafe(32)              # selalu ID baru: cegah session fixation
        db.add(UserSession(
            id=hash_token(raw_sid), user_id=user.id, role=user.role, username=user.username,
            expires_at=db_now_plus(settings.SESSION_TTL),
        ))
        await audit.record(db, audit.Actor(user.id, user.username, raw_ip), "login", "Login", detail="Berhasil")
    return raw_sid


async def must_change_password(db: AsyncSession, user_id: int) -> bool:
    async with db.begin():
        return bool(await db.scalar(select(User.must_change_password).where(User.id == user_id)))


async def logout(db: AsyncSession, sid_hash: str) -> None:
    async with db.begin():
        await db.execute(delete(UserSession).where(UserSession.id == sid_hash))


async def change_role(db: AsyncSession, user_id: int, role: str) -> bool:
    """False bila user tidak ada."""
    validate_role(role)
    async with db.begin():
        result = await db.execute(update(User).where(User.id == user_id).values(role=role))
        # Session menyimpan salinan role: cabut semua agar role baru langsung berlaku
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
    return result.rowcount > 0


class UserNotFound(LookupError):
    pass


async def update_user(
    db: AsyncSession, user_id: int, *, full_name: str, role: str, is_active: bool, actor: audit.Actor,
) -> User:
    """Ubah nama, role, dan status sekaligus. Perubahan role/status mencabut semua session user itu."""
    validate_role(role)
    async with db.begin():
        user = await db.get(User, user_id, with_for_update=True)
        if user is None:
            raise UserNotFound
        validate_identity(user.username, full_name)
        changes = []
        if user.role != role:
            changes.append(f"Role: {user.role} → {role}")
        if user.is_active != is_active:
            changes.append(f"Status: {'aktif' if user.is_active else 'nonaktif'} → "
                           f"{'aktif' if is_active else 'nonaktif'}")
        if user.full_name != full_name:
            changes.append("Nama diubah")
        if user.role != role or user.is_active != is_active:
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        user.full_name, user.role, user.is_active = full_name, role, is_active
        if changes:
            await audit.record(db, actor, "user", "Ubah user", sensitive=True, target_type="user",
                               target_id=user.id, target_label=user.username, detail=" · ".join(changes))
        await db.flush()
        await db.refresh(user)
    return user


async def reset_password(db: AsyncSession, user_id: int, new_password: str, actor: audit.Actor) -> None:
    """Admin mengganti password user lain: user wajib menggantinya lagi saat login berikutnya."""
    await change_password(db, user_id, new_password, must_change=True, actor=actor)


async def change_password(
    db: AsyncSession, user_id: int, new_password: str, *, must_change: bool = False,
    actor: audit.Actor | None = None,
) -> None:
    validate_password(new_password)
    password_hash = await hash_password(new_password)   # di luar transaksi
    async with db.begin():
        result = await db.execute(
            update(User).where(User.id == user_id)
            .values(password_hash=password_hash, must_change_password=must_change)
        )
        if result.rowcount == 0:
            raise UserNotFound
        # Cabut semua session: session yang dicuri sebelum ganti password tidak boleh tetap berlaku.
        # Pemanggil yang ingin user tetap login membuat session baru setelahnya (lewat login()).
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        if actor:
            label = await db.scalar(select(User.username).where(User.id == user_id))
            action = "Ganti password" if actor.user_id == user_id else "Reset password"
            await audit.record(db, actor, "user", action, sensitive=True, target_type="user",
                               target_id=user_id, target_label=label)



async def must_change_password_for_session(db: AsyncSession, sid_hash: str) -> bool:
    async with db.begin():
        return bool(await db.scalar(
            select(User.must_change_password).join(UserSession, UserSession.user_id == User.id)
            .where(UserSession.id == sid_hash)
        ))


async def check_password(db: AsyncSession, user_id: int, password: str, ip: str) -> bool:
    """Cek password saat ini (ganti password). Batasnya sama dengan login, dan berbagi hitungan per (user, IP):
    session yang dicuri tidak bisa dipakai menebak password pemiliknya tanpa batas. Pola catat-dulu seperti login()."""
    raw_ip, ip = ip, rate_limit_key(ip)
    async with db.begin():
        db.add(LoginFailure(user_id=user_id, ip=ip))
    async with db.begin():
        user = await db.get(User, user_id)
        if user is None:
            return False
        attempts = await db.scalar(select(func.count()).where(
            LoginFailure.user_id == user_id, LoginFailure.ip == ip,
            LoginFailure.created_at > db_now_plus(-settings.LOGIN_FAILURE_WINDOW),
        ))
        if attempts == settings.LOGIN_MAX_FAILURES + 1:
            await audit.record(db, audit.Actor(user.id, user.username, raw_ip), "login",
                               f"Password lama salah ×{settings.LOGIN_MAX_FAILURES}", sensitive=True,
                               detail=f"Dikunci {settings.LOGIN_FAILURE_WINDOW // 60} mnt untuk IP ini")
        password_hash = user.password_hash
    if attempts > settings.LOGIN_MAX_FAILURES:
        raise TooManyAttempts
    if not await verify_password(password_hash, password):         # di luar transaksi
        return False
    async with db.begin():
        await db.execute(delete(LoginFailure).where(LoginFailure.ip == ip, LoginFailure.user_id == user_id))
    return True
