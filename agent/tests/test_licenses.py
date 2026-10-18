"""packaging/licenses.py: teks lisensi dependency yang ikut di-bundle installer."""
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import licenses  # noqa: E402


def test_runtime_closure_follows_dependencies_but_not_dev_tools():
    names = {licenses._key(d.metadata["Name"]) for d in licenses.runtime_distributions()}
    assert {"ronda-agent", "httpx", "httpcore", "h11", "psutil", "cryptography", "cffi", "mss", "pillow"} <= names
    assert not names & {"pytest", "ruff", "pyinstaller"}


def test_render_contains_every_component_with_license_text():
    text, count = licenses.render(pyinstaller=False)
    dists = [d for d in licenses.runtime_distributions() if licenses._key(d.metadata["Name"]) != "ronda-agent"]
    assert count == len(dists) + 2                  # + Ronda + Python
    assert "PolyForm Noncommercial License 1.0.0" in text
    assert "PYTHON SOFTWARE FOUNDATION LICENSE" in text
    for dist in dists:
        assert f"\n{dist.metadata['Name']} {dist.version}" in text


class _FakeDist:
    def __init__(self, files):
        self.files = [PurePosixPath(f) for f in files]


def test_license_files_prefers_dist_info_then_falls_back_to_package_files():
    meta = _FakeDist(["foo/__init__.py", "foo-1.0.dist-info/METADATA", "foo-1.0.dist-info/licenses/LICENSE",
                      "foo-1.0.dist-info/licenses/vendor/NOTICE", "foo/LICENSE.txt"])
    assert [f.as_posix() for f in licenses.license_files(meta)] == [
        "foo-1.0.dist-info/licenses/LICENSE", "foo-1.0.dist-info/licenses/vendor/NOTICE"]
    # pywin32: tidak ada di .dist-info, ada di dalam paket
    pkg = _FakeDist(["win32/license.txt", "win32/win32api.pyd", "pywin32-308.dist-info/METADATA"])
    assert [f.as_posix() for f in licenses.license_files(pkg)] == ["win32/license.txt"]
    assert licenses.license_files(_FakeDist(["bar/__init__.py", "bar-1.0.dist-info/METADATA"])) == []
