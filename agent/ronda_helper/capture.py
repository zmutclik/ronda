"""Ambil layar monitor utama (mss) dan kompres ke JPEG yang muat di batas server."""
import io

from PIL import Image

# (lebar maks, kualitas): percobaan kedua bila hasil pertama melebihi batas (dokumen client, "Alur screenshot")
ATTEMPTS = ((1600, 70), (1280, 50))


class CaptureError(Exception):
    pass


def grab_primary() -> Image.Image:
    import mss
    import mss.exception
    try:
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])       # [0] = gabungan semua monitor, [1] = monitor utama
    except mss.exception.ScreenShotError as err:
        # BitBlt ditolak: layar terkunci, UAC (secure desktop), atau sesi sedang terputus
        raise CaptureError("Layar tidak bisa diambil (terkunci atau sesi tidak aktif)") from err
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def encode(image: Image.Image, max_width: int, max_bytes: int) -> tuple[bytes, int, int]:
    for limit, quality in ATTEMPTS:
        width = min(limit, max_width, image.width)
        resized = image if width == image.width else image.resize(
            (width, max(1, round(image.height * width / image.width))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue(), resized.width, resized.height
    raise CaptureError("Gambar tetap melebihi batas ukuran walau sudah diperkecil")


def screenshot(max_width: int, max_bytes: int) -> tuple[bytes, int, int]:
    return encode(grab_primary(), max_width, max_bytes)
