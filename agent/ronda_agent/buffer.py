"""Antrean sampel metrik di SQLite: baris dihapus hanya setelah server membalas 200."""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ronda_agent.schemas import iso

MAX_AGE = timedelta(days=2)      # server membuang sampel lebih tua dari 2 hari
MAX_ROWS = 6000                  # per tabel; ±2 hari sampel 30 detik


class Buffer:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), isolation_level=None)   # autocommit; tiap operasi transaksi sendiri
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (ts TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS disks (ts TEXT NOT NULL, mount TEXT NOT NULL, body TEXT NOT NULL,"
                        " PRIMARY KEY (ts, mount))")

    def close(self) -> None:
        self.db.close()

    def add_sample(self, sample: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO samples VALUES (?, ?)", (sample["ts"], json.dumps(sample)))

    def add_disks(self, disks: list[dict]) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR REPLACE INTO disks VALUES (?, ?, ?)",
                                [(d["ts"], d["mount"], json.dumps(d)) for d in disks])

    def peek(self, limit: int) -> tuple[list[dict], list[dict]]:
        """Batch tertua (maks `limit` per jenis)."""
        samples = [json.loads(b) for (b,) in self.db.execute("SELECT body FROM samples ORDER BY ts LIMIT ?", (limit,))]
        disks = [json.loads(b) for (b,) in self.db.execute("SELECT body FROM disks ORDER BY ts LIMIT ?", (limit,))]
        return samples, disks

    def remove(self, samples: list[dict], disks: list[dict]) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.executemany("DELETE FROM samples WHERE ts = ?", [(s["ts"],) for s in samples])
            self.db.executemany("DELETE FROM disks WHERE ts = ? AND mount = ?", [(d["ts"], d["mount"]) for d in disks])

    def counts(self) -> tuple[int, int]:
        return (self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
                self.db.execute("SELECT COUNT(*) FROM disks").fetchone()[0])

    def prune(self, now: datetime) -> int:
        """Buang yang lebih tua dari 2 hari, lalu yang tertua bila lebih dari MAX_ROWS. Kembalikan jumlah terbuang."""
        cutoff = iso(now - MAX_AGE)
        dropped = 0
        with self.db:
            self.db.execute("BEGIN")
            for table in ("samples", "disks"):
                dropped += self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,)).rowcount
                dropped += self.db.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                    (MAX_ROWS,)).rowcount
        return dropped
