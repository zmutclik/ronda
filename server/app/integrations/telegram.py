"""Adapter Telegram Bot API. Tanpa akses DB; client & timeout dari pemanggil."""
import httpx

TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class SendError(RuntimeError):
    pass


async def send_message(client: httpx.AsyncClient, bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token:
        raise SendError("TELEGRAM_BOT_TOKEN belum diisi.")
    resp = await client.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        # Jangan sertakan URL (berisi token bot) di pesan error yang masuk log/DB
        raise SendError(f"Telegram membalas HTTP {resp.status_code}: {resp.text[:200]}")
