"""Folder terbatas: folder & PC di dalamnya tidak terlihat (di mana pun) oleh user tanpa akses, termasuk Admin lain."""
import httpx
import pytest

from app.alerts.evaluate import evaluate_all
from app.auth.audit import Actor
from app.core.database import async_session_factory
from app.folders import service as folder_service
from app.main import app
from tests.conftest import htmx_headers, login, make_user, online_host, sql
from tests.test_alerts import add_rule
from tests.test_hosts import folder


@pytest.fixture
async def other():
    """Client kedua: Admin lain yang tidak punya akses."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def restricted_setup(client, other):
    """owner membatasi unit Server (berisi SRV2 dan ruangan Rak); budi Admin lain tanpa akses. Kembalikan id-id."""
    owner = await make_user("owner", role="admin")
    budi = await make_user("budi", role="admin")
    unit = await folder("Server")
    room = await folder("Rak", unit)
    hidden_host, _ = await online_host(client, "SRV2", folder_id=room, disks=[{"mount": "C:", "used_pct": 97}])
    visible_host, _ = await online_host(client, "IGD-PC01", disks=[{"mount": "C:", "used_pct": 97}])
    owner_csrf = await login(client, "owner")
    resp = await client.post(f"/hosts/folders/{unit}/access", data={"restricted": "1"}, headers=htmx_headers(owner_csrf))
    assert resp.status_code == 204, resp.text
    budi_csrf = await login(other, "budi")
    return {"owner": owner, "budi": budi, "unit": unit, "room": room, "hidden": hidden_host, "visible": visible_host,
            "owner_csrf": owner_csrf, "budi_csrf": budi_csrf}


async def test_hidden_folder_and_host_invisible_to_other_admin(client, other):
    s = await restricted_setup(client, other)

    # Pemilik akses melihat semuanya
    page = (await client.get("/hosts?group=none")).text
    assert "SRV2" in page and "IGD-PC01" in page
    assert (await client.get(f"/hosts/{s['hidden']}")).status_code == 200
    folders = await client.get("/hosts/folders")
    assert folders.status_code == 200 and "🔒 Server" in folders.text and "Simpan akses" in folders.text

    # Admin lain: daftar, pencarian, grup per folder, detail, dashboard, folder
    assert "SRV2" not in (await other.get("/hosts?group=none")).text
    assert "SRV2" not in (await other.get("/hosts?q=SRV")).text
    page = (await other.get("/hosts?group=folder")).text
    assert "Server" not in page and "1 PC" in page
    assert (await other.get(f"/hosts/rows?unit={s['unit']}")).status_code == 404
    assert (await other.get(f"/hosts/{s['hidden']}")).status_code == 404
    assert (await other.get(f"/hosts/{s['hidden']}/charts")).status_code == 404
    assert "IGD-PC01" in (await other.get("/hosts?group=none")).text
    assert "Server" not in (await other.get("/hosts/folders")).text
    assert "SRV2" not in (await other.get("/dashboard")).text
    nav = (await other.get("/dashboard/nav-counts")).text
    assert ">1<" in nav and ">2<" not in nav

    # Laporan "Semua folder" & folder tersembunyi
    assert "SRV2" not in (await other.get("/reports?report=inventory")).text
    assert (await other.get(f"/reports?report=inventory&folder={s['unit']}")).status_code == 404
    assert "SRV2" in (await client.get("/reports?report=inventory")).text


async def test_other_admin_cannot_act_on_hidden_host_or_folder(client, other):
    s = await restricted_setup(client, other)
    h = htmx_headers(s["budi_csrf"])
    host = s["hidden"]
    assert (await other.post(f"/hosts/{host}/inventory", headers=h)).status_code == 404
    assert (await other.post(f"/hosts/{host}/specs", data={"hostname": "X"}, headers=h)).status_code == 404
    assert (await other.post(f"/hosts/{host}/revoke", headers=h)).status_code == 404
    assert (await other.post(f"/hosts/{host}/screenshots", data={"reason": "cek"}, headers=h)).status_code == 404
    assert (await other.post(f"/remote/hosts/{host}", data={"mode": "control", "reason": "cek"},
                             headers=h)).status_code == 404
    resp = await other.post("/hosts/move", data={"host_id": host, "folder_id": ""}, headers=h)
    assert "0 PC dipindahkan" in resp.headers["HX-Trigger"]
    # PC terlihat tidak bisa dipindah ke folder tersembunyi
    resp = await other.post("/hosts/move", data={"host_id": s["visible"], "folder_id": s["room"]}, headers=h)
    assert resp.status_code == 422
    # Folder tersembunyi: ubah, hapus, buat ruangan di dalamnya, ubah akses
    assert (await other.post(f"/hosts/folders/{s['unit']}", data={"name": "X"}, headers=h)).status_code == 422
    assert (await other.post(f"/hosts/folders/{s['room']}/delete", headers=h)).status_code == 422
    assert (await other.post("/hosts/folders", data={"name": "Baru", "parent_id": s["unit"]},
                             headers=h)).status_code == 422
    assert (await other.post(f"/hosts/folders/{s['unit']}/access", data={"restricted": "0"},
                             headers=h)).status_code == 422
    assert (await sql("SELECT folder_id, status FROM hosts WHERE id = :h", h=host))[0] == (s["room"], "online")
    assert (await sql("SELECT is_restricted, name FROM folders WHERE id = :f", f=s["unit"]))[0] == (1, "Server")


async def test_alerts_of_hidden_host_invisible_and_untouchable(client, other):
    rule_id = await add_rule()
    s = await restricted_setup(client, other)
    await evaluate_all(async_session_factory)
    hidden_alert = (await sql("SELECT id FROM alerts WHERE host_id = :h", h=s["hidden"]))[0][0]

    assert "SRV2" in (await client.get("/alerts")).text
    page = (await other.get("/alerts")).text
    assert "SRV2" not in page and "IGD-PC01" in page
    assert "SRV2" not in (await other.get("/alerts/export.csv?range=30d")).text
    resp = await other.post("/alerts/ack", data={"alert_id": hidden_alert}, headers=htmx_headers(s["budi_csrf"]))
    assert "0 alert diakui" in resp.headers["HX-Trigger"]
    assert (await sql("SELECT status FROM alerts WHERE id = :a", a=hidden_alert))[0][0] == "new"
    # Aturan khusus folder tersembunyi tidak terlihat / tidak bisa diubah
    scoped = await add_rule(metric="cpu_pct", folder_id=s["unit"])
    assert "Aturan cpu_pct" not in (await other.get("/alerts?tab=rules")).text
    assert (await other.post(f"/alerts/rules/{scoped}/toggle", data={"active": "0"},
                             headers=htmx_headers(s["budi_csrf"]))).status_code == 422
    assert rule_id


async def test_audit_log_hides_entries_about_hidden_folder_and_host(client, other):
    s = await restricted_setup(client, other)
    await client.post(f"/hosts/{s['hidden']}/inventory", headers=htmx_headers(s["owner_csrf"]))
    await client.post(f"/hosts/{s['visible']}/inventory", headers=htmx_headers(s["owner_csrf"]))
    # (Enrollment key test bernama "K-SRV2": target key, bukan PC, jadi tetap terlihat; dicek per aksi.)
    mine = (await client.get("/audit")).text
    assert "Ubah akses folder" in mine and mine.count("Refresh inventaris") == 2
    theirs = (await other.get("/audit")).text
    assert "Ubah akses folder" not in theirs and theirs.count("Refresh inventaris") == 1
    assert "IGD-PC01" in theirs and "Login" in theirs          # catatan lain (termasuk tanpa target) tetap ada
    csv_rows = (await other.get("/audit/export.csv")).text.splitlines()
    assert not [r for r in csv_rows if ",SRV2," in r]
    assert [r for r in (await client.get("/audit/export.csv")).text.splitlines() if ",SRV2," in r]


async def test_access_holder_account_locked_from_other_admin(client, other):
    s = await restricted_setup(client, other)
    form = {"full_name": "Owner", "role": "admin", "is_active": "1", "new_password": "password-baru-panjang"}
    resp = await other.post(f"/users/{s['owner'].id}", data=form, headers=htmx_headers(s["budi_csrf"]))
    assert resp.status_code == 403
    form["is_active"] = ""
    assert (await other.post(f"/users/{s['owner'].id}", data=form,
                             headers=htmx_headers(s["budi_csrf"]))).status_code == 403
    assert (await sql("SELECT is_active FROM users WHERE id = :u", u=s["owner"].id))[0][0] == 1
    # Pemegang akses tetap bisa mengubah admin lain (tidak ada folder yang tersembunyi baginya)
    resp = await client.post(f"/users/{s['budi'].id}", data={"full_name": "Budi S", "role": "admin", "is_active": "1"},
                             headers=htmx_headers(s["owner_csrf"]))
    assert resp.status_code == 200


async def test_access_list_always_keeps_editor_and_grants_others(client, other):
    s = await restricted_setup(client, other)
    # Mencoba menghapus diri sendiri: tetap ikut di daftar; menambah budi → budi melihat folder
    resp = await client.post(f"/hosts/folders/{s['unit']}/access", data={"restricted": "1", "user_id": s["budi"].id},
                             headers=htmx_headers(s["owner_csrf"]))
    assert resp.status_code == 204
    users = {r[0] for r in await sql("SELECT user_id FROM folder_access WHERE folder_id = :f", f=s["unit"])}
    assert users == {s["owner"].id, s["budi"].id}
    assert (await other.get(f"/hosts/{s['hidden']}")).status_code == 200
    # Tidak terbatas lagi: daftar akses dikosongkan, semua user melihat
    await client.post(f"/hosts/folders/{s['unit']}/access", data={}, headers=htmx_headers(s["owner_csrf"]))
    assert await sql("SELECT user_id FROM folder_access") == []
    teknisi = await make_user("tek", role="teknisi")
    async with async_session_factory() as db, db.begin():
        assert await folder_service.hidden_folder_ids(db, teknisi.id) == frozenset()


async def test_hidden_set_includes_rooms_and_restricted_room_alone():
    unit = await folder("Server")
    room = await folder("Rak", unit)
    other_unit = await folder("IGD")
    other_room = await folder("Triase", other_unit)
    owner = await make_user("owner", role="admin")
    tek = await make_user("tek", role="teknisi")
    actor = Actor(owner.id, owner.username)
    async with async_session_factory() as db:
        await folder_service.set_access(db, unit, restricted=True, user_ids=[], hidden=frozenset(), actor=actor)
    async with async_session_factory() as db:
        await folder_service.set_access(db, other_room, restricted=True, user_ids=[tek.id], hidden=frozenset(),
                                        actor=actor)
    async with async_session_factory() as db, db.begin():
        assert await folder_service.hidden_folder_ids(db, tek.id) == {unit, room}
        assert await folder_service.hidden_folder_ids(db, owner.id) == frozenset()     # pengubah selalu ikut
        assert await folder_service.hidden_folder_ids(db, None) == frozenset()        # sistem melihat semua
