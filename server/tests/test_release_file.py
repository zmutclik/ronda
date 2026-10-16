"""Penyimpanan file rilis (tanpa database)."""
import errno
import io

import pytest

from app.agents.service import AgentError, _save_release_file


class _DiskFull(io.BytesIO):
    def read(self, size=-1):
        if self.tell():
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().read(4)


def test_save_release_file_ok(tmp_path):
    sha, size = _save_release_file(io.BytesIO(b"abc"), tmp_path / "r.zip", 10)
    assert size == 3 and (tmp_path / "r.zip").read_bytes() == b"abc"
    assert sha == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_save_release_file_too_big_leaves_nothing(tmp_path):
    with pytest.raises(AgentError, match="melebihi"):
        _save_release_file(io.BytesIO(b"x" * 20), tmp_path / "r.zip", 10)
    assert list(tmp_path.iterdir()) == []


def test_save_release_file_disk_full_is_reported(tmp_path):
    with pytest.raises(AgentError, match="No space left on device"):
        _save_release_file(_DiskFull(b"xxxxxxxx"), tmp_path / "r.zip", 100)
    assert list(tmp_path.iterdir()) == []
