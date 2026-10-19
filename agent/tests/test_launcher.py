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


def test_old_versions_cleaned_again_after_every_update(tmp_path):
    """Pembersihan bukan sekali seumur instalasi: setelah update berikutnya stabil, versi lama ikut dihapus."""
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        (tmp_path / "versions" / version).mkdir(parents=True)
    (tmp_path / "current").write_text("1.1.0")
    (tmp_path / "launcher.json").write_text(json.dumps({"previous": "1.0.0", "cleaned": True, "stable_since": 0}))
    clock = [100.0]
    launcher = Launcher(tmp_path, fake_command, stable_sec=10, now=lambda: clock[0])

    (tmp_path / "pending_switch").write_text("1.2.0")
    launcher.apply_pending_switch()
    (tmp_path / "healthy").write_text("x")
    launcher._end_trial_if_healthy()
    clock[0] += 11
    launcher.cleanup_old_versions()
    assert {d.name for d in (tmp_path / "versions").iterdir()} == {"1.1.0", "1.2.0"}


@pytest.mark.parametrize("content", ["..\\\\evil", "bukan-versi", "\xff\xfe"])
def test_corrupt_current_file_falls_back_to_installed_version(launch, content):
    data_dir, start = launch
    install(data_dir, "1.0.0", "healthy")
    (data_dir / "current").write_bytes(content.encode("latin-1"))
    start()
    wait_for(lambda: runs(data_dir) == ["1.0.0"])
    assert current(data_dir) == "1.0.0"


# ---------- pemilik file (folder ProgramData diduduki user biasa sebelum instalasi) ----------

USER_SID = "S-1-5-21-1111-2222-3333-1001"


def owners(*planted: Path):
    """owner_of palsu: path di bawah salah satu `planted` milik user biasa, sisanya SYSTEM."""
    def owner_of(path: Path) -> str:
        return USER_SID if any(path == p or p in path.parents for p in planted) else "S-1-5-18"
    return owner_of


def test_untrusted_reason_checks_whole_tree_and_links(tmp_path):
    from ronda_launcher.trust import untrusted_reason
    install(tmp_path, "1.0.0", "healthy")
    vdir = tmp_path / "versions" / "1.0.0"
    assert untrusted_reason(tmp_path, vdir, owners()) is None
    dll = vdir / "sub" / "version.dll"                 # DLL titipan di folder versi yang sah
    dll.parent.mkdir()
    dll.write_bytes(b"MZ")
    assert "version.dll" in untrusted_reason(tmp_path, vdir, owners(dll))
    assert "bukan SYSTEM" in untrusted_reason(tmp_path, vdir, owners(tmp_path))    # data dir milik user
    (vdir / "link").symlink_to(tmp_path)
    assert "symlink" in untrusted_reason(tmp_path, vdir, owners())


def test_planted_higher_version_is_not_run(launch):
    """User membuat versions\\99.0.0 + current=99.0.0 sebelum agent dipasang: launcher tetap menjalankan versi sah."""
    data_dir, start = launch
    install(data_dir, "1.0.0", "healthy")
    install(data_dir, "99.0.0", "healthy")
    (data_dir / "current").write_text("99.0.0")
    start(owner_of=owners(data_dir / "versions" / "99.0.0"))
    wait_for(lambda: runs(data_dir) == ["1.0.0"])
    assert current(data_dir) == "1.0.0"


def test_pending_switch_to_untrusted_version_is_ignored(launch):
    data_dir, start = launch
    install(data_dir, "1.0.0", "update:1.1.0")
    install(data_dir, "1.1.0", "healthy")
    (data_dir / "current").write_text("1.0.0")
    start(owner_of=owners(data_dir / "versions" / "1.1.0" / "fake_agent.py"))
    wait_for(lambda: runs(data_dir)[:2] == ["1.0.0", "1.0.0"])        # update ditolak, versi lama dijalankan lagi
    assert current(data_dir) == "1.0.0" and "1.1.0" not in runs(data_dir)


def test_nothing_runs_when_data_dir_owned_by_user(launch):
    data_dir, start = launch
    install(data_dir, "1.0.0", "healthy")
    start(owner_of=owners(data_dir))
    time.sleep(1)
    assert runs(data_dir) == []


def test_installer_secures_data_dir_before_copying_files():
    """Penjaga regresi installer.iss (tidak bisa dikompilasi di CI Linux): folder data diamankan di PrepareToInstall
    (sebelum [Files]), dan "instalasi baru" ditentukan dari service, bukan dari config.toml yang bisa dititipkan."""
    iss = (Path(__file__).resolve().parents[1] / "packaging" / "installer.iss").read_text(encoding="utf-8")
    prepare = iss[iss.index("function PrepareToInstall"):iss.index("procedure SetAcl")]
    assert "Result := SecureDataDir();" in prepare
    assert "FreshInstall := not ServiceExists();" in iss
    assert "FileExists(DataDir() + '\\config.toml')" not in iss
    secure = iss[iss.index("function SecureDataDir"):iss.index("function PrepareToInstall")]
    assert "RenameFile(D, Quarantine)" in secure and "/R" not in secure      # isi titipan tidak diproses rekursif
    assert "WizardSilent and FreshInstall" in iss
    assert "CreateInputQueryPage" in iss
    assert "procedure InitializeWizard" in iss
    assert "function NextButtonClick" in iss
