"""Pemasangan lewat skrip: /install.sh, /install.ps1, unduhan installer, dan unggah installer oleh admin."""
import hashlib
import shutil
import subprocess

import pytest

from app.agents.router import render_install_script
from app.core.config import settings
from tests.conftest import htmx_headers, login, make_user, sql

EXE = b"MZ" + b"\0" * 100
TARGZ = b"\x1f\x8b" + b"\0" * 100


@pytest.fixture
def release_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RELEASE_DIR", tmp_path)
    return tmp_path


async def _upload(client, csrf, platform, version, content, filename="x.bin"):
    return await client.post("/agents/releases/installers", data={"platform": platform, "version": version},
                             files={"package": (filename, content)}, headers=htmx_headers(csrf))


async def test_scripts_without_installer_fail_with_clear_message(client):
    for path, platform in (("/install.sh", "Linux"), ("/install.ps1", "Windows")):
        resp = await client.get(path)
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
        assert f"server belum punya installer {platform}" in resp.text
        assert "SERVER='http://test'" in resp.text or "$Server = 'http://test'" in resp.text
        assert "__" not in resp.text.replace("__SERVER__", "")     # semua placeholder terisi


async def test_admin_uploads_installers_and_scripts_use_latest(client, release_dir):
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    resp = await _upload(client, csrf, "windows", "1.2.0", EXE)
    assert resp.status_code == 200 and "Installer windows v1.2.0 diunggah" in resp.headers["hx-trigger"]
    assert (await _upload(client, csrf, "linux", "1.2.0", TARGZ)).status_code == 200
    assert (await _upload(client, csrf, "linux", "1.3.0", TARGZ + b"baru")).status_code == 200
    page = (await client.get("/agents")).text
    assert "curl -fsSL http://test/install.sh | sudo RONDA_KEY=" in page
    assert "irm http://test/install.ps1 | iex" in page and "dipakai skrip" in page

    ps1 = (await client.get("/install.ps1")).text
    win_id, win_sha = (await sql("SELECT id, sha256 FROM agent_installers WHERE platform = 'windows'"))[0]
    assert f"$PackageUrl = 'http://test/install/files/{win_id}'" in ps1
    assert f"$PackageSha256 = '{win_sha}'" in ps1 and "$ServerError = ''" in ps1

    sh = (await client.get("/install.sh")).text
    lin_id, lin_sha = (await sql("SELECT id, sha256 FROM agent_installers WHERE version = '1.3.0'"))[0]
    assert f"PACKAGE_URL='http://test/install/files/{lin_id}'" in sh and "PACKAGE_VERSION='1.3.0'" in sh
    assert lin_sha == hashlib.sha256(TARGZ + b"baru").hexdigest()

    client.cookies.clear()                                   # unduhan publik, tanpa login
    resp = await client.get(f"/install/files/{lin_id}")
    assert resp.status_code == 200 and resp.content == TARGZ + b"baru"
    assert "ronda-agent-linux-x86_64-1.3.0.tar.gz" in resp.headers["content-disposition"]
    assert (await client.get("/install/files/9999")).status_code == 404

    audit = await sql("SELECT action, is_sensitive, target_label FROM audit_logs WHERE target_type = 'installer'")
    assert ("Unggah installer", 1, "linux v1.3.0") in audit


async def test_upload_rejects_wrong_file_duplicate_and_non_release_users(client, release_dir):
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    resp = await _upload(client, csrf, "windows", "1.0.0", TARGZ)
    assert resp.status_code == 422 and "bukan installer windows" in resp.text
    assert (await _upload(client, csrf, "linux", "1.0", TARGZ)).status_code == 422
    assert (await _upload(client, csrf, "mac", "1.0.0", TARGZ)).status_code == 422
    assert (await _upload(client, csrf, "linux", "1.0.0", TARGZ)).status_code == 200
    resp = await _upload(client, csrf, "linux", "1.0.0", TARGZ)
    assert resp.status_code == 422 and "sudah ada" in resp.text
    assert sorted(p.name for p in (release_dir / "installers").iterdir()) == ["ronda-agent-linux-x86_64-1.0.0.tar.gz"]

    await make_user("tek", role="teknisi")
    csrf = await login(client, "tek")
    assert (await _upload(client, csrf, "linux", "2.0.0", TARGZ)).status_code == 403


@pytest.mark.parametrize("server", ["http://x'; rm -rf /", "https://a b", "https://host/path", "https://h:99999x"])
def test_unsafe_server_url_never_reaches_script(server):
    for name in ("install.sh", "install.ps1"):
        script = render_install_script(name, server, None)
        assert server not in script and "alamat server tidak valid" in script


@pytest.mark.skipif(not shutil.which("bash"), reason="butuh bash")
def test_install_sh_is_valid_bash_and_checks_root():
    script = render_install_script("install.sh", "https://ronda.example.local", None)
    assert subprocess.run(["bash", "-n"], input=script.encode(), check=False).returncode == 0
    out = subprocess.run(["bash", "-s", "--", "--bogus"], input=script.encode(), capture_output=True, check=False)
    assert out.returncode == 1 and b"argumen tidak dikenal" in out.stderr
