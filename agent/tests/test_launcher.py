"""Launcher dengan proses agent palsu sungguhan (subprocess Python), masa uji dipersingkat ke hitungan detik."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from ronda_launcher import LAUNCHER_VERSION
from ronda_launcher.core import Launcher

FAKE_AGENT = r'''
import sys, time, pathlib
vdir = pathlib.Path(__file__).parent
data = pathlib.Path(sys.argv[1])
with (data / "runs.log").open("a") as f:
    f.write(vdir.name + "\n")
mode = (vdir / "mode").read_text().strip()
if mode.startswith("update:"):                      # minta pindah versi sekali, lalu berperilaku sehat
    flag = data / ("updated-" + vdir.name)
    if not flag.exists():
        flag.write_text("1")
        (data / "healthy").write_text("x")
        (data / "pending_switch").write_text(mode.split(":")[1])
        sys.exit(75)
    mode = "healthy"
if mode == "healthy":
    (data / "healthy").write_text("x")
    time.sleep(60)
elif mode == "crash":
    sys.exit(3)
elif mode == "silent":                              # jalan tapi tidak pernah heartbeat sukses
    time.sleep(60)
'''


def fake_command(version_dir: Path, data_dir: Path) -> list[str]:
    return [sys.executable, str(version_dir / "fake_agent.py"), str(data_dir)]


def install(data_dir: Path, version: str, mode: str) -> None:
    d = data_dir / "versions" / version
    d.mkdir(parents=True, exist_ok=True)
    (d / "fake_agent.py").write_text(FAKE_AGENT)
    (d / "mode").write_text(mode)


def runs(data_dir: Path) -> list[str]:
    try:
        return (data_dir / "runs.log").read_text().split()
    except FileNotFoundError:
        return []


def state(data_dir: Path) -> dict:
    return json.loads((data_dir / "launcher.json").read_text())


def current(data_dir: Path) -> str:
    return (data_dir / "current").read_text()


def wait_for(cond, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if cond():
                return
        except (FileNotFoundError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    raise AssertionError("kondisi tidak tercapai")


@pytest.fixture
def launch(tmp_path):
    started = []

    def start(**kwargs):
        opts = dict(trial_sec=3.0, crash_window_sec=10.0, crash_limit=3, poll_sec=0.05, restart_delays=(0.05,))
        launcher = Launcher(tmp_path, fake_command, **(opts | kwargs))
        stop = threading.Event()
        thread = threading.Thread(target=launcher.run, args=(stop,), daemon=True)
        thread.start()
        started.append((stop, thread))
        return launcher

    yield tmp_path, start
    for stop, thread in started:
        stop.set()
        thread.join(20)
        assert not thread.is_alive(), "launcher tidak berhenti"


def test_bootstrap_runs_installed_version(launch):
    data_dir, start = launch
    install(data_dir, "1.0.0", "healthy")
    start()
    wait_for(lambda: runs(data_dir) == ["1.0.0"])
    assert current(data_dir) == "1.0.0"
    assert state(data_dir)["launcher_version"] == LAUNCHER_VERSION


def test_update_switches_and_ends_trial_when_healthy(launch):
    data_dir, start = launch
    install(data_dir, "1.0.0", "update:1.1.0")
    install(data_dir, "1.1.0", "healthy")
    (data_dir / "current").write_text("1.0.0")
    start()
    wait_for(lambda: state(data_dir).get("stable_since"))
    s = state(data_dir)
    assert current(data_dir) == "1.1.0" and runs(data_dir) == ["1.0.0", "1.1.0"]
    assert s["previous"] == "1.0.0" and s["trial"] is None and s["update_status"] == "installed"
    assert not (data_dir / "pending_switch").exists()


def test_crashing_version_rolls_back(launch):
    data_dir, start = launch
    install(data_dir, "1.0.0", "update:1.1.0")
    install(data_dir, "1.1.0", "crash")
    (data_dir / "current").write_text("1.0.0")
    start()
    wait_for(lambda: state(data_dir).get("update_status") == "rollback")
    wait_for(lambda: runs(data_dir)[-1] == "1.0.0")
    assert runs(data_dir) == ["1.0.0", "1.1.0", "1.1.0", "1.1.0", "1.0.0"]   # 3 crash lalu kembali
    s = state(data_dir)
    assert current(data_dir) == "1.0.0" and s["bad_versions"] == ["1.1.0"] and s["trial"] is None


def test_never_healthy_version_rolls_back_after_trial(launch):
    data_dir, start = launch
    install(data_dir, "1.0.0", "update:1.1.0")
    install(data_dir, "1.1.0", "silent")
    (data_dir / "current").write_text("1.0.0")
    start(trial_sec=1.0)
    wait_for(lambda: state(data_dir).get("update_status") == "rollback")
    assert current(data_dir) == "1.0.0"
    assert state(data_dir)["bad_versions"] == ["1.1.0"]


def test_crash_outside_trial_restarts_same_version(launch):
    data_dir, start = launch
    install(data_dir, "1.0.0", "crash")
    start()
    wait_for(lambda: len(runs(data_dir)) >= 4)
    assert set(runs(data_dir)) == {"1.0.0"}
    assert state(data_dir).get("update_status") is None


def test_old_versions_removed_after_stable(launch):
    data_dir, start = launch
    install(data_dir, "0.9.0", "healthy")
    install(data_dir, "1.0.0", "update:1.1.0")
    install(data_dir, "1.1.0", "healthy")
    (data_dir / "current").write_text("1.0.0")
    start(stable_sec=0.5)
    wait_for(lambda: not (data_dir / "versions" / "0.9.0").exists())
    assert {d.name for d in (data_dir / "versions").iterdir()} == {"1.0.0", "1.1.0"}


def test_stop_terminates_agent(launch, tmp_path):
    data_dir, start = launch
    install(data_dir, "1.0.0", "healthy")
    launcher = start()
    wait_for(lambda: (data_dir / "healthy").exists())
    assert launcher.current == "1.0.0"
    # fixture menghentikan launcher; thread wajib selesai (agent di-terminate, bukan ditunggu 60 dtk)
