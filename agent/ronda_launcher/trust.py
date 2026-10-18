"""Pemeriksaan pemilik file sebelum launcher (SYSTEM) menjalankan exe dari C:\\ProgramData\\Ronda.

C:\\ProgramData bisa ditulisi user biasa. Bila folder Ronda (atau isinya) dibuat user sebelum agent terpasang, user itu
tetap PEMILIK-nya walau ACL kemudian diganti installer, dan pemilik selalu bisa mengubah ACL lagi: exe/DLL miliknya
akan dijalankan sebagai SYSTEM. Installer kini memindahkan folder seperti itu ke samping; pemeriksaan ini lapis
kedua (juga untuk PC yang terpasang sebelum perbaikan itu): data dir, versions\\, dan SELURUH isi folder versi wajib
dimiliki SYSTEM/Administrators, tanpa symlink/junction (rilis asli tidak pernah memuatnya).
"""
import os
import sys
from collections.abc import Callable
from pathlib import Path

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
TRUSTED_OWNERS = frozenset({SYSTEM_SID, ADMINISTRATORS_SID})

OwnerOf = Callable[[Path], str]


def windows_owner(path: Path) -> str:
    """SID pemilik file/folder (tanpa mengikuti symlink: pemanggil menolak link lebih dulu)."""
    import win32security
    sd = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                            win32security.OWNER_SECURITY_INFORMATION)
    return win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())


def default_owner_of() -> OwnerOf | None:
    """Windows: pemilik asli. Di luar Windows (development/test) tidak ada pemeriksaan."""
    return windows_owner if sys.platform == "win32" else None


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def untrusted_reason(data_dir: Path, version_dir: Path, owner_of: OwnerOf) -> str | None:
    """None bila aman dijalankan; selain itu alasan penolakan (untuk log)."""
    def check(path: Path) -> str | None:
        if _is_link(path):
            return f"{path} adalah symlink/junction"
        try:
            owner = owner_of(path)
        except OSError as err:
            return f"pemilik {path} tidak bisa dibaca: {err}"
        if owner not in TRUSTED_OWNERS:
            return f"pemilik {path} ({owner}) bukan SYSTEM/Administrators"
        return None

    for path in (data_dir, version_dir.parent, version_dir):
        if reason := check(path):
            return reason
    pending = [version_dir]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if reason := check(path):
                    return reason
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
    return None
