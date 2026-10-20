from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DEBUG: bool = False
    DATABASE_URL: str                    # mysql+asyncmy://user:pass@host:3306/db
    # Signing saja (token CSRF), bukan kunci enkripsi. Kosong/pendek ditolak saat start, bukan diam-diam dipakai.
    SECRET_KEY: str = Field(min_length=32)

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_STATEMENT_TIMEOUT: float = 2.5    # engine web (OLTP)

    HOME_PATH: str = "/dashboard"        # tujuan setelah login dan GET /
    DISPLAY_TIMEZONE: str = "Asia/Jakarta"   # hanya untuk tampilan (filter `localtime`); DB tetap UTC
    ORG_NAME: str = "RS Contoh"

    SESSION_TTL: int = 60 * 30                   # idle timeout: diperpanjang selama user aktif
    SESSION_MAX_AGE: int = 60 * 60 * 12          # batas mutlak sejak login (juga max_age cookie)
    COOKIE_SECURE: bool = True
    LOGIN_MAX_FAILURES: int = 5          # per pasangan (username, IP)
    LOGIN_MAX_FAILURES_PER_IP: int = 50  # per IP untuk semua username; longgar agar kantor di balik NAT tidak terblokir
    LOGIN_FAILURE_WINDOW: int = 15 * 60
    PASSWORD_HASH_CONCURRENCY: int = 4   # argon2 paralel per proses; tiap hash ±64 MiB RAM

    # Agent
    SCREENSHOT_DIR: Path = Path("/srv/data/screenshots")   # volume terpisah; file tidak pernah di-serve statis
    RELEASE_DIR: Path = Path("/srv/data/releases")         # paket agent (zip) yang diunggah admin
    SCREENSHOT_MAX_BYTES: int = 3 * 1024 * 1024
    RELEASE_MAX_BYTES: int = 200 * 1024 * 1024
    REMOTE_MAX_DURATION: int = 60 * 60   # sesi remote diputus relay setelah ini (detik)

    # Notifikasi (kosong = kanal jenis itu tidak mengirim apa pun)
    TELEGRAM_BOT_TOKEN: str = ""
    WHATSAPP_API_URL: str = ""           # mis. http://whatsapp:3000 (go-whatsapp-web-multidevice)
    WHATSAPP_API_USER: str = ""
    WHATSAPP_API_PASSWORD: str = ""


settings = Settings()
