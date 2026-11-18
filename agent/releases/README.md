# Rilis agent (auto-update)

Paket rilis bertanda tangan untuk auto-update agent Windows, satu pasang file per versi:

- `ronda-agent-<versi>.zip` — paket dari `packaging/build.ps1`
- `ronda-agent-<versi>.json` — versi, ukuran, SHA-256, tanda tangan Ed25519, catatan

Keduanya dibuat `build.ps1 -Key ...` (atau `sign_release.py sign --publish releases`). Alur:

1. Mesin build: `build.ps1 -Version 1.3.0 -Key E:\release-utama.pem -Notes "..."`, lalu commit + push folder ini.
2. Server: `git pull && ./deploy/upgrade.sh`. Worker (folder ini di-mount read-only) mendaftarkan versi baru dalam
   ±1 menit dan memulai versi terbaru di Ring 0.
3. Naik ring otomatis: Ring 0 → 1 setelah 24 jam, Ring 1 → 2 setelah 72 jam (`RELEASE_RING0_HOURS`,
   `RELEASE_RING1_HOURS`), selama tidak ada PC yang rollback. Ada rollback → rilis dijeda; admin memeriksa lalu
   Lanjutkan atau Hentikan di Pendaftaran Agent › Rilis. `RELEASE_AUTO_PROMOTE=false` mematikan naik ring otomatis.

Versi yang sudah terdaftar tidak dibaca ulang: jangan mengubah file versi lama, naikkan versi. Versi yang tidak lebih
baru dari rilis yang sudah ada hanya didaftarkan, tidak dimulai. File di atas 100 MB ditolak GitHub. Paket lama boleh
dihapus dari folder ini (server sudah menyimpan salinannya), tetapi tetap ada di riwayat git.
