from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from app.auth.service import hash_token
from app.core.database import async_session_factory
from app.hosts.models import Host


@dataclass(frozen=True)
class AgentHost:
    id: int
    hostname: str
    status: str
    ring: int
    agent_version: str | None


async def get_agent(request: Request) -> AgentHost:
    """Autentikasi agent: header `Authorization: Bearer <token>`; DB hanya menyimpan SHA-256 token.
    Session DB sendiri (seperti dependency auth), agar handler bebas membuka db.begin()."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Token agent tidak ada.")
    async with async_session_factory() as db, db.begin():
        row = (await db.execute(
            select(Host.id, Host.hostname, Host.status, Host.ring, Host.agent_version)
            .where(Host.agent_token_hash == hash_token(token.strip()))
        )).one_or_none()
    if row is None or row.status in ("rejected", "blocked"):
        raise HTTPException(status_code=401, detail="Token agent tidak valid atau sudah dicabut.")
    return AgentHost(*row)


async def get_approved_agent(agent: Annotated[AgentHost, Depends(get_agent)]) -> AgentHost:
    if agent.status == "pending":
        raise HTTPException(status_code=403, detail="PC menunggu persetujuan admin.")
    return agent


AgentDep = Annotated[AgentHost, Depends(get_agent)]
ApprovedAgentDep = Annotated[AgentHost, Depends(get_approved_agent)]
