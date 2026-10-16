"""Adapter WhatsApp lewat go-whatsapp-web-multidevice (REST, basic auth). Tanpa akses DB."""
import re

import httpx

from app.integrations.telegram import SendError

TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def normalize_phone(target: str) -> str:
    """08xx / +628xx / 628xx → 628xx@s.whatsapp.net; JID grup (…@g.us) dipakai apa adanya."""
    if "@" in target:
        return target
    digits = re.sub(r"\D", "", target)
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return f"{digits}@s.whatsapp.net"


async def send_message(
    client: httpx.AsyncClient, base_url: str, user: str, password: str, target: str, text: str,
) -> None:
    if not base_url:
        raise SendError("WHATSAPP_API_URL belum diisi.")
    resp = await client.post(
        f"{base_url.rstrip('/')}/send/message",
        json={"phone": normalize_phone(target), "message": text},
        auth=(user, password) if user else None,
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise SendError(f"WhatsApp API membalas HTTP {resp.status_code}: {resp.text[:200]}")
