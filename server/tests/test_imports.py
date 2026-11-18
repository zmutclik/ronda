"""Setiap proses (web, relay, worker) meng-import modulnya dari interpreter baru: import melingkar yang tersembunyi
oleh urutan import test suite tetap ketahuan (relay pernah gagal start karena agents.service ↔ hosts.ingest)."""
import os
import subprocess
import sys

import pytest

from tests.conftest import PROJECT_DIR


@pytest.mark.parametrize("module", ["app.main", "app.remote.relay", "app.jobs.worker", "app.agents.tasks",
                                    "app.agents.service", "app.hosts.ingest", "app.hosts.tasks"])
def test_entry_module_imports_in_fresh_interpreter(module):
    result = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=PROJECT_DIR, env=os.environ.copy(),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
