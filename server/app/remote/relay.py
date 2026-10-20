"""Relay WebSocket sesi remote: memasangkan WebSocket browser (noVNC) dengan WebSocket agent lalu menyalin byte RFB
dua arah. Relay tidak mengurai RFB; jabat tangan & password VNC diurus agent di PC (lihat agent ronda_agent/remote.py),
jadi password VNC tidak pernah sampai ke browser maupun server.

WAJIB satu proses (pasangan sesi disimpan di memori proses): service `relay` di docker-compose, diarahkan nginx dari
/relay/. Development (DEBUG, satu proses uvicorn --reload): rute yang sama ikut dipasang di app web (app/main.py).

    uvicorn app.remote.relay:app --workers 1

- Browser: cookie session login (pemilik sesi + hak akses mode) dan header Origin harus host yang sama.
- Agent:   header Authorization: Bearer <agent_token>, sesi harus milik PC itu.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.agents.dependencies import get_agent
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import async_session_factory, engine
from app.core.discovery import import_domain_modules
from app.core.logging import setup_logging
from app.remote import service
from app.remote.service import PICKUP_WINDOW, RelayTicket

log = logging.getLogger(__name__)

AGENT_WAIT = 60        # agent sudah tersambung, browser belum (tab ditutup sebelum agent datang)
POLL_SEC = 3           # cek tombol Akhiri / kegagalan agent / batas durasi
# Kode tutup WebSocket (4000–4999 bebas dipakai aplikasi); dibaca remote.js untuk pesan ke user
CLOSE_AUTH, CLOSE_GONE, CLOSE_DUPLICATE, CLOSE_TIMEOUT, CLOSE_ENDED = 4401, 4404, 4409, 4408, 4410


@dataclass
class _Slot:
    ticket: RelayTicket
    agent: WebSocket | None = None
    browser: WebSocket | None = None
    paired: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


_slots: dict[int, _Slot] = {}

ws_router = APIRouter(prefix="/relay")


def _same_origin(ws: WebSocket) -> bool:
    """Cegah cross-site WebSocket hijacking: cookie ikut terkirim walau halaman pembuka dari situs lain.
    Hanya hostname yang dibandingkan: proxy di depan (NPM, nginx `$host`) membuang port dari header Host."""
    origin = urlsplit(ws.headers.get("origin", "")).hostname
    host = urlsplit("//" + ws.headers.get("host", "")).hostname
    return bool(origin) and origin == host


async def _ticket(session_id: int) -> RelayTicket | None:
    async with async_session_factory() as db:
        return await service.open_ticket(db, session_id)


@ws_router.websocket("/browser/{session_id}")
async def browser(ws: WebSocket, session_id: int):
    if not _same_origin(ws):
        return await ws.close(CLOSE_AUTH)
    try:
        user = await get_current_user(ws)       # type: ignore[arg-type]  (hanya header, cookie, url, state)
    except HTTPException:
        return await ws.close(CLOSE_AUTH)
    ticket = await _ticket(session_id)
    needed = "remote_control" if ticket and ticket.mode == "control" else "remote_view"
    if ticket is None or ticket.user_id != user.id or not user.can(needed):
        return await ws.close(CLOSE_GONE)
    await ws.accept()
    await _join(ticket, "browser", ws, PICKUP_WINDOW)


@ws_router.websocket("/agent/{session_id}")
async def agent(ws: WebSocket, session_id: int):
    try:
        host = await get_agent(ws)              # type: ignore[arg-type]
    except HTTPException:
        return await ws.close(CLOSE_AUTH)
    ticket = await _ticket(session_id)
    if ticket is None or ticket.host_id != host.id:
        return await ws.close(CLOSE_GONE)
    await ws.accept()
    await _join(ticket, "agent", ws, AGENT_WAIT)


async def _join(ticket: RelayTicket, side: str, ws: WebSocket, wait: float) -> None:
    slot = _slots.setdefault(ticket.id, _Slot(ticket))
    if getattr(slot, side) is not None:          # tab kedua untuk sesi yang sama, atau agent mengulang
        return await ws.close(CLOSE_DUPLICATE)
    setattr(slot, side, ws)
    if slot.agent and slot.browser:              # sisi kedua yang datang menjalankan sesi
        del _slots[ticket.id]
        slot.paired.set()
        try:
            await _run(slot)
        finally:
            slot.done.set()
        return
    outcome = await _wait_partner(slot, wait)
    if outcome == "paired":
        await slot.done.wait()                   # sisi lain sedang menjalankan sesi; koneksi ini tetap terbuka
        return
    if _slots.get(ticket.id) is slot:
        del _slots[ticket.id]
    await _close(ws, CLOSE_ENDED if outcome == "ended" else CLOSE_TIMEOUT)
    if outcome == "timeout":
        await _finish(ticket.id, "timeout")
    log.info("remote_unpaired", extra={"session_id": ticket.id, "side": side, "outcome": outcome})


async def _wait_partner(slot: _Slot, wait: float) -> str:
    """Sisi ini tidak dibaca selama menunggu: agent sudah mengirim versi RFB, yang harus sampai ke browser utuh."""
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        try:
            await asyncio.wait_for(slot.paired.wait(), POLL_SEC)
            return "paired"
        except TimeoutError:
            pass
        if await _ended(slot.ticket.id):         # Akhiri ditekan, atau agent melapor gagal
            return "ended"
        if asyncio.get_running_loop().time() >= deadline:
            return "timeout"


async def _run(slot: _Slot) -> None:
    sid = slot.ticket.id
    agent_ws, browser_ws = slot.agent, slot.browser
    async with async_session_factory() as db:
        if not await service.mark_connected(db, sid):
            await asyncio.gather(_close(agent_ws, CLOSE_ENDED), _close(browser_ws, CLOSE_ENDED))
            return
    log.info("remote_connected", extra={"session_id": sid, "mode": slot.ticket.mode})
    tasks = [asyncio.create_task(_pump(agent_ws, browser_ws, "agent_closed", "browser_closed")),
             asyncio.create_task(_pump(browser_ws, agent_ws, "browser_closed", "agent_closed")),
             asyncio.create_task(_watch(sid))]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    reason = next(iter(done)).result()
    try:
        code = CLOSE_TIMEOUT if reason == "max_duration" else CLOSE_ENDED
        await asyncio.gather(_close(agent_ws, code), _close(browser_ws, code))
    finally:
        await _finish(sid, reason)
    log.info("remote_ended", extra={"session_id": sid, "reason": reason})


async def _pump(src: WebSocket, dst: WebSocket, src_gone: str, dst_gone: str) -> str:
    while True:
        try:
            message = await src.receive()
        except (WebSocketDisconnect, RuntimeError):
            return src_gone
        if message["type"] == "websocket.disconnect":
            return src_gone
        data = message.get("bytes")
        if data is None:                         # RFB selalu biner; teks diabaikan
            continue
        try:
            await dst.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return dst_gone


async def _watch(session_id: int) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.REMOTE_MAX_DURATION
    while True:
        await asyncio.sleep(POLL_SEC)
        if await _ended(session_id):
            return "stopped"                     # alasan sebenarnya sudah tercatat pihak yang menutup
        if loop.time() >= deadline:
            return "max_duration"


async def _ended(session_id: int) -> bool:
    async with async_session_factory() as db:
        return await service.ended(db, session_id)


async def _finish(session_id: int, reason: str) -> None:
    async with async_session_factory() as db:
        await service.finish(db, session_id, reason)


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code)
    except (WebSocketDisconnect, RuntimeError, OSError):      # sudah tertutup
        pass


# ---------- proses relay ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with async_session_factory() as db:
        if closed := await service.close_orphans(db):
            log.warning("remote_orphans_closed", extra={"count": closed})
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    setup_logging()
    import_domain_modules("models")
    relay = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    relay.include_router(ws_router)

    @relay.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    return relay


app = create_app()
