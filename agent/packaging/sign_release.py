"""Alat rilis agent (dijalankan di mesin build; kunci privat tidak pernah masuk repo atau server).

    python packaging/sign_release.py keygen --name utama --out E:\\        # sekali: kunci utama (USB 1)
    python packaging/sign_release.py keygen --name cadangan --out F:\\     # sekali: kunci cadangan (USB 2)
    python packaging/sign_release.py build --version 1.3.0 --min-launcher 1.0.0 \\
        --agent-dir dist/ronda-agent --helper-dir dist/ronda-helper --out ronda-agent-1.3.0.zip
    python packaging/sign_release.py sign --key E:\\release-utama.pem ronda-agent-1.3.0.zip
    python packaging/sign_release.py pubkey --key E:\\release-utama.pem   # kunci publik dari file kunci privat
    python packaging/sign_release.py verify --signature <base64> ronda-agent-1.3.0.zip

Tanda tangan = Ed25519 atas byte zip mentah, base64. Tempel hasil `sign` ke form Rilis di menu Pendaftaran Agent.
Kunci privat dienkripsi passphrase (diminta saat keygen/sign; non-interaktif: env RONDA_RELEASE_PASSPHRASE).
Prosedur kunci produksi: agent/README.md "Kunci tanda tangan rilis".
"""
import argparse
import base64
import getpass
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ronda_agent.release import ReleaseError, verify_package, version_key  # noqa: E402
from ronda_agent.release_keys import PUBLIC_KEYS  # noqa: E402

FIXED_TIME = (2020, 1, 1, 0, 0, 0)    # zip deterministik: build ulang dari isi sama → SHA-256 sama
MIN_PASSPHRASE = 12
PASSPHRASE_ENV = "RONDA_RELEASE_PASSPHRASE"


def public_key_b64(private: Ed25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def key_file(out: Path, name: str = "private") -> Path:
    return out / f"release-{name}.pem"


def keygen(out: Path, name: str = "private", passphrase: bytes | None = None) -> str:
    """Pasangan kunci baru; kunci privat PKCS8 terenkripsi passphrase (None = tanpa enkripsi, hanya untuk uji)."""
    if passphrase is not None and len(passphrase) < MIN_PASSPHRASE:
        raise SystemExit(f"passphrase minimal {MIN_PASSPHRASE} karakter")
    out.mkdir(parents=True, exist_ok=True)
    target = key_file(out, name)
    if target.exists():
        raise SystemExit(f"{target} sudah ada; tidak ditimpa")
    private = Ed25519PrivateKey.generate()
    encryption = (serialization.BestAvailableEncryption(passphrase) if passphrase is not None
                  else serialization.NoEncryption())
    target.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             encryption))
    return public_key_b64(private)


def load_key(key_path: Path, passphrase: bytes | None = None) -> Ed25519PrivateKey:
    """Kunci privat dari file; passphrase None + file terenkripsi → dari env RONDA_RELEASE_PASSPHRASE / prompt."""
    data = key_path.read_bytes()
    if passphrase is None and b"ENCRYPTED" in data:
        env = os.environ.get(PASSPHRASE_ENV)
        passphrase = (env if env is not None else getpass.getpass(f"Passphrase {key_path.name}: ")).encode()
    try:
        private = serialization.load_pem_private_key(data, password=passphrase)
    except (ValueError, TypeError) as err:
        raise SystemExit(f"kunci {key_path} tidak bisa dibuka (passphrase salah?)") from err
    if not isinstance(private, Ed25519PrivateKey):
        raise SystemExit("kunci bukan Ed25519")
    return private


def build(version: str, min_launcher: str, dirs: dict[str, Path], out: Path, *, require_keys: bool = True) -> None:
    """Zip berisi folder per proses + manifest.json."""
    version_key(version)
    version_key(min_launcher)
    if require_keys and not PUBLIC_KEYS:
        raise SystemExit("ronda_agent/release_keys.py masih kosong: build ini tidak akan bisa di-update. Isi dulu.")
    if require_keys and len(PUBLIC_KEYS) < 2:
        print("PERINGATAN: release_keys.py hanya berisi 1 kunci. Bila kunci itu hilang, agent hasil build ini tidak bisa "
              "di-update lagi (pasang ulang manual). Tambahkan kunci cadangan.", file=sys.stderr)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {"version": version, "min_launcher": min_launcher}
        zf.writestr(zipfile.ZipInfo("manifest.json", FIXED_TIME), json.dumps(manifest, indent=1))
        for name, source in dirs.items():
            for path in sorted(p for p in source.rglob("*") if p.is_file()):
                info = zipfile.ZipInfo(f"{name}/{path.relative_to(source).as_posix()}", FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())


def sign(key_path: Path, package: Path, passphrase: bytes | None = None) -> dict:
    private = load_key(key_path, passphrase)
    data = package.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "signature": base64.b64encode(private.sign(data)).decode(), "public_key": public_key_b64(private)}


def main() -> None:
    parser = argparse.ArgumentParser(prog="sign_release")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("keygen")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--name", default="private", help="nama file: release-<name>.pem (mis. utama, cadangan)")
    p.add_argument("--no-passphrase", action="store_true", help="kunci tanpa enkripsi; HANYA untuk uji")
    p = sub.add_parser("build")
    p.add_argument("--version", required=True)
    p.add_argument("--min-launcher", required=True)
    p.add_argument("--agent-dir", type=Path, required=True)
    p.add_argument("--helper-dir", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("sign")
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("package", type=Path)
    p = sub.add_parser("pubkey")
    p.add_argument("--key", type=Path, required=True)
    p = sub.add_parser("verify")
    p.add_argument("--signature", required=True)
    p.add_argument("package", type=Path)
    args = parser.parse_args()

    if args.cmd == "keygen":
        passphrase = None
        if not args.no_passphrase:
            passphrase = os.environ.get(PASSPHRASE_ENV) or getpass.getpass("Passphrase baru: ")
            if PASSPHRASE_ENV not in os.environ and getpass.getpass("Ulangi passphrase: ") != passphrase:
                raise SystemExit("passphrase tidak sama")
            passphrase = passphrase.encode()
        pub = keygen(args.out, args.name, passphrase)
        print(f"Kunci privat: {key_file(args.out, args.name)} (simpan offline, jangan masuk repo/server)")
        print(f"Kunci publik — tambahkan ke PUBLIC_KEYS di ronda_agent/release_keys.py:\n    {pub!r},")
    elif args.cmd == "build":
        dirs = {"ronda-agent": args.agent_dir}
        if args.helper_dir:
            dirs["ronda-helper"] = args.helper_dir
        build(args.version, args.min_launcher, dirs, args.out)
        print(f"Paket: {args.out}")
    elif args.cmd == "sign":
        result = sign(args.key, args.package)
        if result["public_key"] not in PUBLIC_KEYS:
            raise SystemExit("kunci ini tidak ada di ronda_agent/release_keys.py: agent akan menolak paket. "
                             "Pakai kunci utama/cadangan yang tertanam.")
        print(f"SHA-256 : {result['sha256']}\nUkuran  : {result['size']}\nSignature (tempel ke form Rilis):\n"
              f"{result['signature']}")
    elif args.cmd == "pubkey":
        pub = public_key_b64(load_key(args.key))
        status = "tertanam di release_keys.py" if pub in PUBLIC_KEYS else "TIDAK ada di release_keys.py"
        print(f"{pub}  ({status})")
    elif args.cmd == "verify":
        data = args.package.read_bytes()
        try:
            verify_package(args.package, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                           signature_b64=args.signature)
        except ReleaseError as err:
            raise SystemExit(f"GAGAL: {err}") from err
        print("OK: tanda tangan cocok dengan kunci di release_keys.py")


if __name__ == "__main__":
    main()
