"""Paket rilis: verifikasi, ekstraksi, alat sign_release, dan alur update di agent (server palsu)."""
import base64
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ronda_agent.release import (UPDATE_EXIT_CODE, Layout, ReleaseError, check_manifest, extract_package,
                                 verify_package)
from tests.fakes import run_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import sign_release  # noqa: E402

PRIVATE = Ed25519PrivateKey.generate()
OTHER = Ed25519PrivateKey.generate()
PUB = sign_release.public_key_b64(PRIVATE)


def make_zip(version="1.1.0", min_launcher="1.0.0", extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"version": version, "min_launcher": min_launcher}))
        zf.writestr("ronda-agent/ronda-agent.exe", b"MZ fake")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def offer_for(data: bytes, version="1.1.0", key=PRIVATE) -> dict:
    return {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "signature": base64.b64encode(key.sign(data)).decode(), "url": f"/api/agent/releases/{version}/download"}


# ---------- verifikasi ----------

def test_verify_package_accepts_valid_and_rejects_tampering(tmp_path):
    data = make_zip()
    pkg = tmp_path / "p.zip"
    pkg.write_bytes(data)
    o = offer_for(data)
    verify_package(pkg, size=o["size"], sha256=o["sha256"], signature_b64=o["signature"], public_keys=(PUB,))

    cases = [dict(size=o["size"] + 1), dict(sha256="0" * 64), dict(signature_b64=offer_for(data, key=OTHER)["signature"]),
             dict(signature_b64="bukan base64!"), dict(public_keys=())]
    for override in cases:
        kwargs = dict(size=o["size"], sha256=o["sha256"], signature_b64=o["signature"], public_keys=(PUB,)) | override
        with pytest.raises(ReleaseError):
            verify_package(pkg, **kwargs)


def test_signature_covers_raw_bytes(tmp_path):
    """Isi diubah lalu SHA-256 dihitung ulang (server dibobol): tanda tangan tetap menolak."""
    data = make_zip()
    signature = offer_for(data)["signature"]
    tampered = make_zip(extra={"ronda-agent/evil.dll": b"x"})
    pkg = tmp_path / "p.zip"
    pkg.write_bytes(tampered)
    with pytest.raises(ReleaseError, match="Ed25519"):
        verify_package(pkg, size=len(tampered), sha256=hashlib.sha256(tampered).hexdigest(), signature_b64=signature,
                       public_keys=(PUB,))


# ---------- ekstraksi & manifest ----------

@pytest.mark.parametrize("name", ["../evil.txt", "/etc/evil", "C:/Windows/evil.dll", "a/../../evil"])
def test_extract_rejects_path_traversal(tmp_path, name):
    pkg = tmp_path / "p.zip"
    pkg.write_bytes(make_zip(extra={name: b"x"}))
    with pytest.raises(ReleaseError, match="berbahaya"):
        extract_package(pkg, tmp_path / "versions" / "1.1.0")
    assert not (tmp_path / "versions" / "1.1.0").exists()


def test_extract_and_manifest(tmp_path):
    pkg = tmp_path / "p.zip"
    pkg.write_bytes(make_zip(min_launcher="1.2.0"))
    target = tmp_path / "versions" / "1.1.0"
    manifest = extract_package(pkg, target)
    assert (target / "ronda-agent" / "ronda-agent.exe").read_bytes() == b"MZ fake"
    check_manifest(manifest, expected_version="1.1.0", launcher_version="1.2.0")
    with pytest.raises(ReleaseError, match="launcher"):
        check_manifest(manifest, expected_version="1.1.0", launcher_version="1.0.0")
    with pytest.raises(ReleaseError, match="manifest"):
        check_manifest(manifest, expected_version="1.1.1", launcher_version="1.2.0")


def test_layout_rejects_bad_version_names(tmp_path):
    with pytest.raises(ReleaseError):
        Layout(tmp_path).version_dir("..\\..\\Windows")


# ---------- alat rilis ----------

def test_sign_release_build_is_deterministic_and_verifiable(tmp_path):
    src = tmp_path / "dist" / "ronda-agent"
    src.mkdir(parents=True)
    (src / "ronda-agent.exe").write_bytes(b"MZ")
    (src / "_internal").mkdir()
    (src / "_internal" / "python313.dll").write_bytes(b"dll")
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    sign_release.build("1.1.0", "1.0.0", {"ronda-agent": src}, a, require_keys=False)
    sign_release.build("1.1.0", "1.0.0", {"ronda-agent": src}, b, require_keys=False)
    assert a.read_bytes() == b.read_bytes()

    keydir = tmp_path / "kunci"
    pub = sign_release.keygen(keydir)
    result = sign_release.sign(keydir / "release-private.pem", a)
    assert result["public_key"] == pub and len(result["signature"]) == 88     # server menerima 40–128 karakter
    verify_package(a, size=result["size"], sha256=result["sha256"], signature_b64=result["signature"],
                   public_keys=(pub,))
    with pytest.raises(SystemExit):
        sign_release.keygen(keydir)                                          # tidak menimpa kunci lama
    with pytest.raises(SystemExit, match="release_keys"):
        sign_release.build("1.1.0", "1.0.0", {"ronda-agent": src}, a)         # kunci tertanam masih kosong


# ---------- alur update di agent ----------

def _online_agent(env, data: bytes, offer: dict, jitter: int = 600):
    data_dir, clock, server, make_agent = env
    (data_dir / "config.toml").write_text(f'server_url = "https://ronda.test"\nupdate_jitter_sec = {jitter}\n')
    server.status = "online"
    server.packages[offer["version"]] = data
    server.update = offer
    agent = make_agent()
    agent.phase, agent.api.token = "online", server.token
    agent.release_keys = (PUB,)
    return agent


def _hb_status(server) -> str:
    return [b for p, b in server.calls if p == "/api/agent/heartbeat"][-1]["update_status"]


def test_update_success_exits_with_75(env):
    data_dir, clock, server, _ = env
    data = make_zip()
    agent = _online_agent(env, data, offer_for(data))
    agent.tick()
    assert agent.update_status == "waiting"
    assert 0 <= agent.due["update_start"] - clock.mono() <= 600     # jeda acak sebelum unduh
    run_for(agent, clock, 35)
    assert _hb_status(server) == "waiting"
    run_for(agent, clock, 600)
    agent.update_job and agent.update_job.thread.join(5)
    agent.tick()
    assert agent.exit_code == UPDATE_EXIT_CODE
    assert (data_dir / "pending_switch").read_text() == "1.1.0"
    assert (data_dir / "versions" / "1.1.0" / "manifest.json").exists()
    assert not list((data_dir / "versions" / "_incoming").iterdir())        # zip sementara dihapus


def test_update_with_bad_signature_reports_failed(env):
    data_dir, clock, server, _ = env
    data = make_zip()
    agent = _online_agent(env, data, offer_for(data, key=OTHER), jitter=0)
    agent.tick()                      # cek update; jeda 0 → unduh langsung mulai
    agent.update_job.thread.join(5)
    clock.advance(30)
    agent.tick()
    assert agent.exit_code is None and agent.update_status == "failed"
    assert _hb_status(server) == "failed"
    assert not (data_dir / "pending_switch").exists()
    assert not (data_dir / "versions" / "1.1.0").exists()


def test_rolled_back_version_is_skipped(env):
    data_dir, clock, server, _ = env
    data = make_zip()
    (data_dir / "launcher.json").write_text(json.dumps({"update_status": "rollback", "bad_versions": ["1.1.0"]}))
    agent = _online_agent(env, data, offer_for(data), jitter=0)
    run_for(agent, clock, 60)
    assert agent.update_offer is None and agent.update_status == "rollback"
    assert _hb_status(server) == "rollback"
    assert not any(p.startswith("/api/agent/releases/") for p in server.paths())


def test_download_url_outside_release_path_is_refused(env):
    data_dir, clock, server, _ = env
    data = make_zip()
    offer = offer_for(data) | {"url": "https://penyerang.example/x.zip"}
    agent = _online_agent(env, data, offer, jitter=0)
    agent.tick()                      # cek update; jeda 0 → unduh langsung mulai
    agent.update_job.thread.join(5)
    agent.tick()
    assert agent.update_status == "failed"
    assert all("penyerang" not in str(p) for p in server.paths())
