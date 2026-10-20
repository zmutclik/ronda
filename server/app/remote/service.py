"""Sesi remote (noVNC → relay → tunnel agent → UltraVNC lokal di PC).

Alur: user meminta sesi (start_session) → perintah `remote` dititipkan di respons heartbeat (hosts/ingest.py) →
agent membuka WebSocket ke relay, browser juga → relay memasangkan keduanya (mark_connected) dan menyalin byte RFB
→ sesi selesai (finish/stop_session/agent_failed). Relay berjalan sebagai SATU proses terpisah (relay.py): dua
WebSocket sesi yang sama harus mendarat di proses yang sama, sedangkan web berjalan dengan beberapa worker.
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.models import User
from app.core.config import settings
from app.core.database import db_now_plus
from app.hosts.ingest import REMOTE_PICKUP_WINDOW
from app.hosts.models import Host, HostEvent, RemoteSession
from app.hosts.service import APPROVED, REMOTE_END_REASONS, live_status

# Agent harus tersambung ke relay dalam waktu ini: perintah ikut heartbeat (bawaan 30 dtk) + menyalakan VNC.
# hosts/ingest.py memakai nilai yang sama untuk menitipkan perintah `remote`.
PICKUP_WINDOW = REMOTE_PICKUP_WINDOW
MODES = {"view": "Lihat saja", "control": "Kontrol penuh"}
END_REASONS = REMOTE_END_REASONS


class RemoteError(ValueError):
    pass


@dataclass
class SessionView:
    session: RemoteSession
    hostname: str
    username: str

    @property
    def state(self) -> str:
        s = self.session
        if s.ended_at is not None:
            return "ended"
        return "active" if s.connected_at is not None else "waiting"


async def start_session(
    db: AsyncSession, host_id: int, *, mode: str, reason: str, actor: audit.Actor, threshold: int,
) -> RemoteSession:
    """Hak akses mode (remote_view / remote_control) dicek router sebelum memanggil ini."""
    if mode not in MODES:
        raise RemoteError("Mode remote tidak dikenal.")
    if not 3 <= len(reason) <= 200:
        raise RemoteError("Alasan wajib diisi (3–200 karakter).")
    async with db.begin():
        row = (await db.execute(select(Host, live_status(threshold)).where(Host.id == host_id)
                                .with_for_update())).one_or_none()
        if row is None or row[0].status not in APPROVED:
            raise RemoteError("PC tidak ditemukan.")
        host, status = row
        if status != "online":
            raise RemoteError("PC sedang offline; remote hanya bisa ke PC yang online.")
        # Baris host terkunci: dua permintaan bersamaan ke PC yang sama tidak bisa sama-sama lolos cek ini
        busy = await db.scalar(
            select(User.username).join(RemoteSession, RemoteSession.user_id == User.id)
            .where(RemoteSession.host_id == host_id, RemoteSession.ended_at.is_(None)).limit(1))
        if busy:
            raise RemoteError(f"PC ini sedang di-remote oleh {busy}.")
        session = RemoteSession(host_id=host_id, user_id=actor.user_id, mode=mode, reason=reason,
                                client_ip=(actor.ip or "")[:45], started_at=func.now())
        db.add(session)
        db.add(HostEvent(host_id=host_id, kind="remote", level="warn",
                         message=f"Remote ({MODES[mode].lower()}) oleh {actor.username} · {reason}"[:255],
                         meta={"ip": actor.ip}))
        await audit.record(db, actor, "remote", "Mulai remote", sensitive=True, target_type="host",
                           target_id=host_id, target_label=host.hostname,
                           detail=f"Mode: {MODES[mode]} · Alasan: {reason}")
        await db.flush()
        await db.refresh(session)
    return session


async def get_session(db: AsyncSession, session_id: int) -> SessionView | None:
    row = (await db.execute(
        select(RemoteSession, Host.hostname, User.username)
        .join(Host, Host.id == RemoteSession.host_id).join(User, User.id == RemoteSession.user_id)
        .where(RemoteSession.id == session_id)
    )).one_or_none()
    return SessionView(*row) if row else None


async def stop_session(db: AsyncSession, session_id: int, actor: audit.Actor) -> bool:
    """Tombol "Akhiri" (pemilik sesi). Relay melihat ended_at terisi dan memutus kedua sisi."""
    async with db.begin():
        session = await db.get(RemoteSession, session_id, with_for_update=True)
        if session is None or session.ended_at is not None:
            return False
        session.ended_at, session.end_reason = func.now(), "stopped"
        hostname = await db.scalar(select(Host.hostname).where(Host.id == session.host_id))
        await audit.record(db, actor, "remote", "Akhiri remote", sensitive=True, target_type="host",
                           target_id=session.host_id, target_label=hostname, detail=f"Sesi #{session_id}")
    return True


async def agent_failed(db: AsyncSession, host_id: int, session_id: int, error: str) -> bool:
    async with db.begin():
        result = await db.execute(
            update(RemoteSession).where(RemoteSession.id == session_id, RemoteSession.host_id == host_id,
                                        RemoteSession.ended_at.is_(None))
            .values(ended_at=func.now(), end_reason="failed", error=error[:200]))
    return result.rowcount > 0


# ---------- relay ----------

@dataclass(frozen=True)
class RelayTicket:
    """Data sesi yang dibutuhkan relay untuk mengizinkan satu sisi tersambung."""
    id: int
    host_id: int
    user_id: int
    mode: str
    started_at: datetime


async def open_ticket(db: AsyncSession, session_id: int) -> RelayTicket | None:
    """Sesi yang masih boleh disambungkan: belum selesai, belum tersambung, dan masih dalam PICKUP_WINDOW."""
    async with db.begin():
        row = (await db.execute(
            select(RemoteSession.id, RemoteSession.host_id, RemoteSession.user_id, RemoteSession.mode,
                   RemoteSession.started_at)
            .where(RemoteSession.id == session_id, RemoteSession.ended_at.is_(None),
                   RemoteSession.connected_at.is_(None),
                   RemoteSession.started_at > db_now_plus(-PICKUP_WINDOW))
        )).one_or_none()
    return RelayTicket(*row) if row else None


async def mark_connected(db: AsyncSession, session_id: int) -> bool:
    async with db.begin():
        result = await db.execute(
            update(RemoteSession).where(RemoteSession.id == session_id, RemoteSession.ended_at.is_(None),
                                        RemoteSession.connected_at.is_(None))
            .values(connected_at=func.now()))
    return result.rowcount > 0


async def ended(db: AsyncSession, session_id: int) -> bool:
    """True juga bila baris sudah tidak ada (PC dihapus → sesi ikut terhapus)."""
    async with db.begin():
        row = (await db.execute(select(RemoteSession.ended_at).where(RemoteSession.id == session_id))).one_or_none()
    return row is None or row.ended_at is not None


async def finish(db: AsyncSession, session_id: int, end_reason: str) -> None:
    """Tutup sesi bila belum ditutup pihak lain (Akhiri/agent gagal tetap menjadi alasan yang tercatat)."""
    async with db.begin():
        await db.execute(update(RemoteSession).where(RemoteSession.id == session_id,
                                                     RemoteSession.ended_at.is_(None))
                         .values(ended_at=func.now(), end_reason=end_reason))


async def close_orphans(db: AsyncSession) -> int:
    """Relay baru mulai: sesi yang tercatat tersambung pasti milik proses relay sebelumnya (sudah putus)."""
    async with db.begin():
        result = await db.execute(
            update(RemoteSession).where(RemoteSession.ended_at.is_(None), RemoteSession.connected_at.is_not(None))
            .values(ended_at=func.now(), end_reason="relay_restart"))
    return result.rowcount


async def expire_stale(db: AsyncSession) -> int:
    """Worker: sesi yang agent-nya tidak pernah tersambung, dan sesi yang melewati batas durasi tanpa ditutup relay
    (relay mati mendadak)."""
    async with db.begin():
        waiting = await db.execute(
            update(RemoteSession).where(RemoteSession.ended_at.is_(None), RemoteSession.connected_at.is_(None),
                                        RemoteSession.started_at < db_now_plus(-(PICKUP_WINDOW + 30)))
            .values(ended_at=func.now(), end_reason="timeout",
                    error="Agent tidak tersambung (PC offline, agent lama, atau relay tidak terjangkau)"))
        stale = await db.execute(
            update(RemoteSession).where(
                RemoteSession.ended_at.is_(None), RemoteSession.connected_at.is_not(None),
                RemoteSession.connected_at < db_now_plus(-(settings.REMOTE_MAX_DURATION + 300)))
            .values(ended_at=func.now(), end_reason="expired"))
    return waiting.rowcount + stale.rowcount
