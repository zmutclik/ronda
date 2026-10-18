"""Kunci publik Ed25519 (raw 32 byte, base64) yang dipercaya untuk paket rilis.

Isi sebelum build 1.0.0 dengan DUA kunci (utama + cadangan) dari `python packaging/sign_release.py keygen`; prosedur
di README "Kunci tanda tangan rilis". Kunci publik boleh di-commit; kunci privat tidak pernah masuk repo.
Rotasi: tambahkan kunci baru di satu rilis, baru pakai untuk menandatangani di rilis berikutnya.
Tuple kosong = build ini menolak semua update; packaging/sign_release.py `build` menolak membangun paket begitu.
"""
PUBLIC_KEYS: tuple[str, ...] = ()
