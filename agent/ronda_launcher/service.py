"""Pembungkus Windows Service (pywin32). Hanya diimpor di Windows; logika ada di core.Launcher.

Installer memasang service `RondaLauncher` dengan `sc create` (start otomatis, LocalSystem, recovery: restart setelah
1 menit, 3×); exe tanpa argumen = dijalankan Service Control Manager.
"""
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from ronda_agent.config import default_data_dir
from ronda_agent.release import Layout
from ronda_launcher.core import Launcher
from ronda_launcher.helper import make_supervisor
from ronda_launcher.main import setup_logging


class RondaLauncherService(win32serviceutil.ServiceFramework):
    _svc_name_ = "RondaLauncher"
    _svc_display_name_ = "Ronda Launcher"
    _svc_description_ = "Menjalankan dan memperbarui agent monitoring Ronda."

    def __init__(self, args):
        super().__init__(args)
        self.stop = threading.Event()
        self.wait_handle = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):  # noqa: N802 (nama dari pywin32)
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30_000)
        self.stop.set()
        win32event.SetEvent(self.wait_handle)

    def SvcDoRun(self):  # noqa: N802
        data_dir = default_data_dir()
        setup_logging(data_dir, console=False)
        servicemanager.LogInfoMsg("RondaLauncher mulai")
        Launcher(data_dir, helper=make_supervisor(Layout(data_dir))).run(self.stop)


def main(argv: list[str]) -> int:
    if not argv:          # dijalankan oleh Service Control Manager
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(RondaLauncherService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(RondaLauncherService, argv=["ronda-launcher", *argv])
    return 0
