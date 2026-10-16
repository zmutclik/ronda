import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select, update

from app.auth.audit import Actor
from app.auth.models import ROLE_LABELS, User, UserSession
from app.auth.permissions import PERMISSIONS
from app.auth.service import hash_token
from app.core.config import settings
from app.core.database import async_session_factory, db_now_plus, db_time_plus
from app.core.dependencies import client_ip

SESSION_COOKIE = "sid"
TOUCH_INTERVAL = 300   # perpanjang session / last_used maks 1x per 5 menit, bukan UPDATE tiap request
PASSWORD_PATH = "/account/password"
# Satu-satunya halaman yang boleh dibuka selama password sementara belum diganti
PASSWORD_CHANGE_ALLOWED = (PASSWORD_PATH, "/logout")


@dataclass(frozen=True)
class CurrentUser:
    id: int
    role: str
    username: str
    sid: str           # hash session ID
    ip: str = ""

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def initials(self) -> str:
        name = self.username.split(".")[-1] if "." in self.username else self.username
        return name[:2].upper()

    def can(self, permission: str) -> bool:
        return self.role in PERMISSIONS[permission]

    @property
    def actor(self) -> Actor:
        return Actor(user_id=self.id, username=self.username, ip=self.ip)


def csrf_token_for(sid: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), sid.encode(), "sha256").hexdigest()


async def get_current_user(request: Request) -> CurrentUser:
    raw_sid = request.cookies.get(SESSION_COOKIE)
    if not raw_sid:
        raise HTTPException(status_code=401)
    sid = hash_token(raw_sid)

    # Session DB milik dependency ini sendiri, BUKAN session request (Depends(get_db)):
    # query di session request memicu autobegin sehingga db.begin() di service gagal.
    async with async_session_factory() as db, db.begin():
        row = (await db.execute(
            select(
                UserSession.user_id,
                UserSession.role,
                UserSession.username,
                User.must_change_password,
                (UserSession.last_seen_at < db_now_plus(-TOUCH_INTERVAL)).label("stale"),
            )
            .join(User, User.id == UserSession.user_id)
            # Lapis kedua selain penghapusan session di update_user: user nonaktif tidak pernah lolos
            .where(UserSession.id == sid, UserSession.expires_at > func.now(), User.is_active.is_(True))
        )).one_or_none()
        if row is None:
            raise HTTPException(status_code=401)
        if row.stale:
            # Idle timeout diperpanjang, tapi tidak melewati batas mutlak sejak login. Cookie tidak perlu
            # diperbarui: max_age-nya sudah SESSION_MAX_AGE, server yang menentukan kapan session berakhir.
            await db.execute(
                update(UserSession).where(UserSession.id == sid)
                .values(last_seen_at=func.now(), expires_at=func.least(
                    db_now_plus(settings.SESSION_TTL),
                    db_time_plus(UserSession.created_at, settings.SESSION_MAX_AGE),
                ))
            )

    # Password sementara (dibuat/di-reset Admin) wajib diganti sebelum memakai halaman lain, bukan hanya
    # redirect sesudah login: tanpa ini URL lain tetap bisa dibuka langsung dengan password sementara.
    if row.must_change_password and request.url.path not in PASSWORD_CHANGE_ALLOWED:
        raise HTTPException(status_code=303, headers={"Location": PASSWORD_PATH})

    request.state.csrf_token = csrf_token_for(sid)
    user = CurrentUser(id=row.user_id, role=row.role, username=row.username, sid=sid, ip=client_ip(request))
    request.state.user = user
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_role(*roles: str):
    async def checker(user: CurrentUserDep) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(status_code=403)
        return user
    return checker


def require_permission(permission: str):
    """Hak akses per aksi (app/auth/permissions.py); 403 bila role tidak termasuk."""
    roles = PERMISSIONS[permission]       # KeyError saat import bila nama hak akses salah ketik
    return require_role(*roles)


async def require_csrf(request: Request, user: CurrentUserDep) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        sent = (await request.form()).get("csrf_token")      # form non-HTMX
    if not isinstance(sent, str) or not hmac.compare_digest(sent, csrf_token_for(user.sid)):
        raise HTTPException(status_code=403, detail="Token CSRF tidak valid.")

