# Ronda

Monitoring komputer client untuk tim IT: agent Windows di setiap PC mengirim heartbeat, metrik, inventaris, dan
screenshot ke server; dashboard web menampilkan kondisi, alert, dan laporan.

| Folder | Isi |
|---|---|
| [`server/`](server/README.md) | FastAPI + Jinja2 + htmx + Alpine + MariaDB: API agent, dashboard, worker periodik, backup harian |
| [`agent/`](agent/README.md) | Agent Windows: launcher (Windows Service, auto-update + rollback), agent, helper screenshot, installer |

Dokumen rancangan: [`rorda_-_dokumen_pengembangan.md`](rorda_-_dokumen_pengembangan.md) (induk: seluruh sistem) dan
[`ronda_agent_-_dokumen_pengembangan_client.md`](ronda_agent_-_dokumen_pengembangan_client.md) (agent).

Test kontrak agent ↔ server (`agent/tests/test_contract.py`) menjalankan server dari folder `server/`, jadi
keduanya disimpan dalam satu repo.

Status: kode Tahap 1–4 selesai kecuali rekap aplikasi lintas PC (F-10), ditambah remote layar (noVNC + UltraVNC,
belum diuji di Windows); belum produksi. Checklist sebelum produksi dan
fitur yang belum dikerjakan ada di [`server/README.md`](server/README.md#status--belum-dikerjakan).

## Lisensi

[PolyForm Noncommercial 1.0.0](LICENSE) — © 2026 Fahrudin Hariadi. Boleh dipakai, diubah, dan dibagikan untuk
keperluan **nonkomersial**, termasuk dipakai sendiri oleh rumah sakit, sekolah, instansi pemerintah, dan organisasi
nirlaba. Menjual software ini atau menjadikannya layanan berbayar memerlukan izin tertulis dari pemegang hak cipta.
Komponen pihak ketiga: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
