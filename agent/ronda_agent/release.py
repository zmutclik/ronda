"""Paket rilis: tata letak folder versi, verifikasi (ukuran, SHA-256, Ed25519) dan ekstraksi.
Dipakai agent (unduh + pasang ke folder baru) dan launcher (pindah versi, rollback)."""
import base64
import hashlib
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ronda_agent.release_keys import PUBLIC_KEYS

UPDATE_EXIT_CODE = 75            # agent keluar dengan kode ini: "versi baru siap, tolong pindah"
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_EXTRACTED_BYTES = 600 * 1024 * 1024


class ReleaseError(Exception):
    pass


def version_key(version: str) -> tuple[int, ...]:
    if not VERSION_RE.match(version):
        raise ReleaseError(f"versi tidak valid: {version!r}")
    return tuple(int(p) for p in version.split("."))


@dataclass(frozen=True)
class Layout:
    """File bersama di C:\\ProgramData\\Ronda (lihat dokumen client, "File di C:\\ProgramData\\Ronda")."""
    data_dir: Path

    @property
    def versions(self) -> Path:
        return self.data_dir / "versions"

    @property
    def incoming(self) -> Path:
        return self.versions / "_incoming"

    @property
    def current_file(self) -> Path:
        return self.data_dir / "current"

    @property
    def pending_switch(self) -> Path:
        return self.data_dir / "pending_switch"

    @property
    def healthy(self) -> Path:
        return self.data_dir / "healthy"

    @property
    def launcher_state(self) -> Path:
        """Ditulis launcher; dibaca agent (versi launcher, hasil update terakhir, versi yang gagal)."""
        return self.data_dir / "launcher.json"

    def version_dir(self, version: str) -> Path:
        version_key(version)          # tolak "..\\x" dsb. sebelum jadi path
        return self.versions / version

    def read_launcher_state(self) -> dict:
        try:
            return json.loads(self.launcher_state.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}


def verify_package(path: Path, *, size: int, sha256: str, signature_b64: str,
                   public_keys: tuple[str, ...] | None = None) -> None:
    """Ukuran = `size`, SHA-256 = `sha256`, dan tanda tangan Ed25519 atas byte zip mentah cocok dengan salah satu
    kunci publik tertanam. Kunci kosong = semua update ditolak (lebih aman daripada menerima paket tanpa cek)."""
    keys = PUBLIC_KEYS if public_keys is None else public_keys
    if not keys:
        raise ReleaseError("kunci publik rilis belum ditanam di build ini; update ditolak")
    actual_size = path.stat().st_size
    if actual_size != size:
        raise ReleaseError(f"ukuran paket {actual_size} ≠ {size}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256.lower():
        raise ReleaseError("SHA-256 paket tidak cocok")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError as err:
        raise ReleaseError("tanda tangan bukan base64") from err
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(signature, data)
            return
        except InvalidSignature:
            continue
    raise ReleaseError("tanda tangan Ed25519 tidak valid")


def extract_package(package: Path, target: Path) -> dict:
    """Ekstrak ke folder sementara lalu rename ke `target`; kembalikan manifest.json.
    Menolak path absolut / `..` (zip slip) dan ukuran ekstrak yang tidak wajar."""
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(package) as zf:
        total = 0
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise ReleaseError(f"path berbahaya di paket: {info.filename!r}")
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise ReleaseError("isi paket terlalu besar")
        tmp.mkdir(parents=True)
        zf.extractall(tmp)
    try:
        manifest = json.loads((tmp / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as err:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ReleaseError("manifest.json tidak ada atau rusak") from err
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)
    return manifest


def check_manifest(manifest: dict, *, expected_version: str, launcher_version: str | None) -> None:
    if manifest.get("version") != expected_version:
        raise ReleaseError(f"manifest versi {manifest.get('version')!r} ≠ rilis {expected_version}")
    min_launcher = manifest.get("min_launcher")
    if min_launcher and launcher_version and version_key(launcher_version) < version_key(min_launcher):
        raise ReleaseError(f"butuh launcher ≥ {min_launcher}, terpasang {launcher_version}")
