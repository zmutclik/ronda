"""Konfigurasi deploy (docker-compose.yml): pembagian kredensial antar-service."""
from pathlib import Path

import yaml

from tests.conftest import PROJECT_DIR

COMPOSE = yaml.safe_load((Path(PROJECT_DIR) / "docker-compose.yml").read_text())
FOREIGN = {
    "web": ("WORKER_DATABASE_URL", "BACKUP_DB_HOST", "BACKUP_DB_USER", "BACKUP_DB_PASSWORD"),
    "worker": ("BACKUP_DB_HOST", "BACKUP_DB_USER", "BACKUP_DB_PASSWORD"),
}


def test_services_with_env_file_blank_other_services_db_credentials():
    """env_file membawa seluruh .env: tanpa penimpaan, web yang dibobol mendapat user worker (DROP partisi
    audit_logs) dan user backup. Nilai di `environment` menimpa env_file."""
    for name, keys in FOREIGN.items():
        service = COMPOSE["services"][name]
        assert service["env_file"] == ".env"
        env = service["environment"]
        assert {k: env.get(k) for k in keys} == dict.fromkeys(keys, ""), name


def test_worker_uses_worker_db_user_and_backup_gets_only_its_vars():
    assert COMPOSE["services"]["worker"]["environment"]["DATABASE_URL"].startswith("${WORKER_DATABASE_URL")
    backup = COMPOSE["services"]["backup"]
    assert "env_file" not in backup
    assert all(k.startswith("BACKUP_") or k == "TZ" for k in backup["environment"])


def test_app_code_mounted_in_web_and_worker():
    expected = {"./app:/srv/app", "./migrations:/srv/migrations", "./alembic.ini:/srv/alembic.ini"}
    for name in ("web", "worker"):
        vols = set(COMPOSE["services"][name]["volumes"])
        assert expected.issubset(vols), name
