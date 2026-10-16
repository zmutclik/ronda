"""Alat rilis agent (dijalankan di mesin build; kunci privat tidak pernah masuk repo atau server).

    python packaging/sign_release.py keygen --out D:\\kunci-rilis          # sekali: buat pasangan kunci
    python packaging/sign_release.py build --version 1.3.0 --min-launcher 1.0.0 \\
        --agent-dir dist/ronda-agent --helper-dir dist/ronda-helper --out ronda-agent-1.3.0.zip
    python packaging/sign_release.py sign --key D:\\kunci-rilis\\release-private.pem ronda-agent-1.3.0.zip
    python packaging/sign_release.py verify --signature <base64> ronda-agent-1.3.0.zip

Tanda tangan = Ed25519 atas byte zip mentah, base64. Tempel hasil `sign` ke form Rilis di menu Pendaftaran Agent.
"""
import argparse
import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ronda_agent.release import ReleaseError, verify_package, version_key  # noqa: E402
from ronda_agent.release_keys import PUBLIC_KEYS  # noqa: E402

FIXED_TIME = (2020, 1, 1, 0, 0, 0)    # zip deterministik: build ulang dari isi sama → SHA-256 sama


def public_key_b64(private: Ed25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def keygen(out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    target = out / "release-private.pem"
    if target.exists():
        raise SystemExit(f"{target} sudah ada; tidak ditimpa")
    private = Ed25519PrivateKey.generate()
    target.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    return public_key_b64(private)


def build(version: str, min_launcher: str, dirs: dict[str, Path], out: Path, *, require_keys: bool = True) -> None:
    """Zip berisi folder per proses + manifest.json."""
    version_key(version)
    version_key(min_launcher)
    if require_keys and not PUBLIC_KEYS:
        raise SystemExit("ronda_agent/release_keys.py masih kosong: build ini tidak akan bisa di-update. Isi dulu.")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {"version": version, "min_launcher": min_launcher}
        zf.writestr(zipfile.ZipInfo("manifest.json", FIXED_TIME), json.dumps(manifest, indent=1))
        for name, source in dirs.items():
            for path in sorted(p for p in source.rglob("*") if p.is_file()):
                info = zipfile.ZipInfo(f"{name}/{path.relative_to(source).as_posix()}", FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())


def sign(key_path: Path, package: Path) -> dict:
    private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise SystemExit("kunci bukan Ed25519")
    data = package.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "signature": base64.b64encode(private.sign(data)).decode(), "public_key": public_key_b64(private)}


def main() -> None:
    parser = argparse.ArgumentParser(prog="sign_release")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("keygen")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("build")
    p.add_argument("--version", required=True)
    p.add_argument("--min-launcher", required=True)
    p.add_argument("--agent-dir", type=Path, required=True)
    p.add_argument("--helper-dir", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("sign")
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("package", type=Path)
    p = sub.add_parser("verify")
    p.add_argument("--signature", required=True)
    p.add_argument("package", type=Path)
    args = parser.parse_args()

    if args.cmd == "keygen":
        pub = keygen(args.out)
        print(f"Kunci privat: {args.out / 'release-private.pem'} (simpan offline, jangan masuk repo)")
        print(f"Kunci publik — tempel ke ronda_agent/release_keys.py:\n    PUBLIC_KEYS = ({pub!r},)")
    elif args.cmd == "build":
        dirs = {"ronda-agent": args.agent_dir}
        if args.helper_dir:
            dirs["ronda-helper"] = args.helper_dir
        build(args.version, args.min_launcher, dirs, args.out)
        print(f"Paket: {args.out}")
    elif args.cmd == "sign":
        result = sign(args.key, args.package)
        if result["public_key"] not in PUBLIC_KEYS:
            print("PERINGATAN: kunci ini tidak ada di release_keys.py build yang terpasang; agent akan menolak paket.",
                  file=sys.stderr)
        print(f"SHA-256 : {result['sha256']}\nUkuran  : {result['size']}\nSignature (tempel ke form Rilis):\n"
              f"{result['signature']}")
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
