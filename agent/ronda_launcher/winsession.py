"""Windows: sesi user konsol dan menjalankan helper di sesi itu sebagai user tersebut (pywin32).

Service berjalan di Session 0 dan tidak bisa melihat desktop user, jadi helper dijalankan dengan token user sesi konsol
(WTSQueryUserToken, butuh hak LocalSystem) di desktop `winsta0\\default`.
"""
import subprocess
from pathlib import Path

import pywintypes
import win32con
import win32event
import win32process
import win32profile
import win32ts

NO_SESSION = 0xFFFFFFFF


def active_console_session() -> int | None:
    """Sesi di monitor fisik, hanya bila ada user login (bukan layar logon)."""
    session = win32ts.WTSGetActiveConsoleSessionId()
    if session == NO_SESSION:
        return None
    try:
        user = win32ts.WTSQuerySessionInformation(win32ts.WTS_CURRENT_SERVER_HANDLE, session, win32ts.WTSUserName)
    except pywintypes.error:
        return None
    return session if user else None


class UserProcess:
    def __init__(self, handle, pid: int):
        self.handle, self.pid = handle, pid

    def poll(self) -> int | None:
        if win32event.WaitForSingleObject(self.handle, 0) != win32event.WAIT_OBJECT_0:
            return None
        return win32process.GetExitCodeProcess(self.handle)

    def terminate(self) -> None:
        if self.poll() is None:
            win32process.TerminateProcess(self.handle, 1)
            win32event.WaitForSingleObject(self.handle, 5000)


def spawn_in_session(session: int, cmd: list[str], cwd: Path, env: dict[str, str]) -> UserProcess:
    token = win32ts.WTSQueryUserToken(session)
    try:
        block = win32profile.CreateEnvironmentBlock(token, False)    # environment user (LOCALAPPDATA, dst.)
        block.update(env)
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "winsta0\\default"
        flags = win32process.CREATE_UNICODE_ENVIRONMENT | win32process.CREATE_NO_WINDOW | \
            win32con.NORMAL_PRIORITY_CLASS
        process, thread, pid, _ = win32process.CreateProcessAsUser(
            token, None, subprocess.list2cmdline(cmd), None, None, False, flags, block, str(cwd), startup)
        thread.Close()
    finally:
        token.Close()
    return UserProcess(process, pid)
