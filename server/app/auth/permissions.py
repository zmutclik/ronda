"""Hak akses per role (matriks di halaman User & Hak Akses). Dua role tetap; perubahan hak akses = ubah kode ini."""

ADMIN_ONLY = frozenset({"admin"})
ALL_ROLES = frozenset({"admin", "teknisi"})

# kunci → role yang boleh. Urutan & label dipakai matriks di halaman users (PERMISSION_MATRIX).
PERMISSIONS: dict[str, frozenset[str]] = {
    "view": ALL_ROLES,                 # dashboard, daftar & detail PC, riwayat per PC
    "pc_manage": ADMIN_ONLY,           # refresh inventaris, pindah folder, ubah data PC
    "remote_view": ALL_ROLES,
    "remote_control": ALL_ROLES,
    "screenshot": ALL_ROLES,
    "alert_handle": ALL_ROLES,         # akui, bisukan, selesaikan
    "alert_rules": ADMIN_ONLY,         # aturan & kanal notifikasi
    "folders_manage": ADMIN_ONLY,      # buat/ubah/hapus folder
    "agent_approve": ALL_ROLES,        # setujui / tolak PC baru (halaman Pendaftaran Agent)
    "agent_enroll": ADMIN_ONLY,        # enrollment key, cabut token agent
    "agent_release": ADMIN_ONLY,
    "users_manage": ADMIN_ONLY,
    "audit_view": ADMIN_ONLY,
    "report_export": ALL_ROLES,
    "report_sensitive": ADMIN_ONLY,    # laporan aktivitas remote & screenshot
}

PERMISSION_MATRIX: list[tuple[str, list[tuple[str, str, bool]]]] = [
    ("Lihat", [
        ("view", "Dashboard, daftar & detail komputer, riwayat per PC", False),
    ]),
    ("Tindakan PC", [
        ("pc_manage", "Refresh inventaris, pindah folder, ubah data PC", False),
        ("remote_view", "Remote · lihat saja", True),
        ("remote_control", "Remote · kontrol penuh", True),
        ("screenshot", "Screenshot", True),
    ]),
    ("Alert", [
        ("alert_handle", "Akui, bisukan, selesaikan alert", False),
        ("alert_rules", "Ubah aturan & kanal notifikasi", False),
    ]),
    ("Admin", [
        ("folders_manage", "Kelola folder unit & ruangan", False),
        ("agent_approve", "Setujui / tolak PC baru", False),
        ("agent_enroll", "Enrollment key, cabut token agent", False),
        ("agent_release", "Rilis & rollback agent", False),
        ("users_manage", "Kelola user & hak akses", True),
        ("audit_view", "Lihat Audit Log", False),
        ("report_export", "Export laporan", False),
    ]),
]
