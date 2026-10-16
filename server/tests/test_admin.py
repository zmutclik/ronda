"""Halaman Admin, hak akses per role, Audit Log, laporan, dan aturan CSP template."""
import re

import pytest

from tests.conftest import enroll, enrollment_key, htmx_headers, login, make_user, online_host, sql

ADMIN_ONLY = ["/agents", "/users", "/users?tab=roles", "/audit", "/audit/export.csv", "/hosts/folders",
              "/reports?report=remote"]
ALL_PAGES = ["/dashboard", "/hosts", "/hosts?group=status", "/hosts?group=none", "/alerts", "/alerts?tab=history",
             "/alerts?tab=rules", "/reports", "/reports?report=availability", "/reports?report=alerts",
             "/reports?report=renewal", "/account/password"]


async def test_teknisi_forbidden_on_admin_pages(client):
    await make_user("tek", role="teknisi")
    await login(client, "tek")
    for path in ADMIN_ONLY:
        assert (await client.get(path)).status_code == 403, path
    for path in ALL_PAGES:
        assert (await client.get(path)).status_code == 200, path


async def test_pages_have_no_inline_script_or_style(client):
    """CSP script-src/style-src 'self': atribut style= atau <script> inline akan diblokir browser."""
    await online_host(client)
    await make_user("adm", role="admin")
    await login(client, "adm")
    host_id = (await sql("SELECT id FROM hosts LIMIT 1"))[0][0]
    pages = ALL_PAGES + ADMIN_ONLY[:-2] + [f"/hosts/{host_id}?tab={t}" for t in
                                           ("summary", "apps", "network", "history", "screenshot", "remote")]
    for path in pages:
        html = (await client.get(path)).text
        assert not re.search(r"\sstyle=", html), path
        assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html), path
        assert not re.search(r"\son[a-z]+=", html), path                     # onclick= dll.


async def test_new_user_must_change_password(client):
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    resp = await client.post("/users", data={"username": "Teknisi.Rina", "full_name": "Rina W", "role": "teknisi",
                                             "password": "sementara-awal-1"}, headers=htmx_headers(csrf))
    assert resp.status_code == 200 and "teknisi.rina" in resp.text
    dup = await client.post("/users", data={"username": "teknisi.rina", "full_name": "X", "role": "teknisi",
                                           "password": "sementara-awal-1"}, headers=htmx_headers(csrf))
    assert dup.status_code == 422
    client.cookies.clear()
    page = await client.get("/login")
    resp = await client.post("/login", data={"username": "teknisi.rina", "password": "sementara-awal-1",
                                             "csrf_token": page.cookies.get("login_csrf")})
    assert resp.status_code == 303 and resp.headers["location"] == "/account/password"
    from app.auth.dependencies import SESSION_COOKIE, csrf_token_for
    from app.auth.service import hash_token
    csrf = csrf_token_for(hash_token(client.cookies[SESSION_COOKIE]))
    # Selama password sementara belum diganti, halaman lain tidak bisa dibuka langsung lewat URL
    for path in ("/dashboard", "/hosts", "/alerts"):
        page = await client.get(path)
        assert page.status_code == 303 and page.headers["location"] == "/account/password", path
    fragment = await client.get("/dashboard", headers={"HX-Request": "true"})
    assert fragment.status_code == 204 and fragment.headers["hx-redirect"] == "/account/password"
    assert (await client.post("/hosts/move", headers=htmx_headers(csrf))).headers["hx-redirect"] == "/account/password"
    assert (await client.get("/account/password")).status_code == 200
    bad = await client.post("/account/password", data={"current": "salah", "new": "baru-panjang-12345", "repeat": "baru-panjang-12345",
                                                       "csrf_token": csrf})
    assert bad.status_code == 422
    ok = await client.post("/account/password", data={"current": "sementara-awal-1", "new": "baru-panjang-12345",
                                                      "repeat": "baru-panjang-12345", "csrf_token": csrf})
    assert ok.status_code == 303 and ok.headers["location"] == "/login"
    assert (await sql("SELECT must_change_password FROM users WHERE username = 'teknisi.rina'"))[0][0] == 0


async def test_deactivating_user_revokes_sessions_and_blocks_login(client):
    await make_user("adm", role="admin")
    tek = await make_user("tek", role="teknisi")
    csrf = await login(client, "adm")
    assert (await client.post(f"/users/{tek.id}", data={"full_name": "Tek", "role": "teknisi"},
                              headers=htmx_headers(csrf))).status_code == 200
    assert (await sql("SELECT is_active FROM users WHERE id = :i", i=tek.id))[0][0] == 0
    # Admin tidak bisa menonaktifkan dirinya sendiri
    me = (await sql("SELECT id FROM users WHERE username = 'adm'"))[0][0]
    assert (await client.post(f"/users/{me}", data={"full_name": "A", "role": "admin"},
                              headers=htmx_headers(csrf))).status_code == 422
    client.cookies.clear()
    page = await client.get("/login")
    resp = await client.post("/login", data={"username": "tek", "password": "s3cret-pass-panjang",
                                             "csrf_token": page.cookies.get("login_csrf")})
    assert resp.status_code == 400
    detail = (await sql("SELECT detail FROM audit_logs WHERE action = 'Ubah user'"))[0][0]
    assert "aktif → nonaktif" in detail


async def test_session_of_inactive_user_is_rejected(client):
    """Lapis kedua: session yang (karena sebab apa pun) masih ada tidak berlaku setelah user dinonaktifkan."""
    tek = await make_user("tek", role="teknisi")
    await login(client, "tek")
    assert (await client.get("/dashboard")).status_code == 200
    from sqlalchemy import text

    from app.core.database import engine
    async with engine.begin() as conn:                                         # tanpa menghapus user_sessions
        await conn.execute(text("UPDATE users SET is_active = 0 WHERE id = :i"), {"i": tek.id})
    resp = await client.get("/dashboard")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


async def test_agent_hub_approve_with_folder_and_keys(client):
    from tests.test_hosts import folder
    unit = await folder("Rawat Inap")
    room = await folder("Anggrek", unit, "10.10.22.0/24")
    data = await enroll(client, await enrollment_key(), "DESKTOP-7H2KQ1", "10.10.22.57")
    await make_user("adm", role="admin")
    csrf = await login(client, "adm")
    hub = await client.get("/agents")
    assert "Saran: Rawat Inap › Anggrek" in hub.text
    resp = await client.post("/agents/approve", data={"host_id": data["host_id"], f"folder_{data['host_id']}": room},
                             headers=htmx_headers(csrf))
    assert resp.status_code == 200
    assert (await sql("SELECT status, folder_id FROM hosts WHERE id = :i", i=data["host_id"]))[0] == ("online", room)
    key = await client.post("/agents/keys", data={"prefix": "rs-2027", "valid_days": "30"}, headers=htmx_headers(csrf))
    raw = re.search(r'id="new-key">([A-Z0-9-]+)<', key.text).group(1)
    assert raw.startswith("RS-2027-")
    assert raw not in str(await sql("SELECT key_hash, key_prefix FROM enrollment_keys"))   # hanya hash tersimpan


async def test_audit_log_filters_and_csv(client):
    await make_user("adm", role="admin")
    await login(client, "adm")
    page = await client.get("/audit?range=today&category=login")
    assert page.status_code == 200 and "Login" in page.text
    csv = await client.get("/audit/export.csv?range=7d")
    assert csv.headers["content-type"].startswith("text/csv") and "Waktu,User" in csv.text.lstrip("﻿")
    assert (await client.get("/audit?range=custom&from=bukan-tanggal")).status_code == 422


@pytest.mark.parametrize("report", ["inventory", "availability", "alerts", "renewal", "remote"])
async def test_reports_preview_and_download(client, report):
    await online_host(client)
    await make_user("adm", role="admin")
    await login(client, "adm")
    assert (await client.get(f"/reports?report={report}")).status_code == 200
    xlsx = await client.get(f"/reports/download?report={report}")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    printed = await client.get(f"/reports/print?report={report}")
    assert printed.status_code == 200 and 'data-action="print"' in printed.text
    assert [r[0] for r in await sql("SELECT format FROM report_downloads")] == ["xlsx", "pdf"]
