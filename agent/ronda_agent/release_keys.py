"""Kunci publik Ed25519 (raw 32 byte, base64) yang dipercaya untuk paket rilis.

Isi dengan output `python packaging/sign_release.py keygen` sebelum build 1.0.0. Lebih dari satu kunci boleh
(rotasi: tambahkan kunci baru di satu rilis, baru pakai untuk menandatangani di rilis berikutnya).
Tuple kosong = build ini menolak semua update; packaging/sign_release.py `build` menolak membangun paket begitu.
"""
PUBLIC_KEYS: tuple[str, ...] = ()
