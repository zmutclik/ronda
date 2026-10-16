"""ronda-launcher: di Windows berjalan sebagai service RondaLauncher (service.py); `console` untuk development.

    ronda-launcher console --data-dir data          # jalan di terminal sampai Ctrl+C
    ronda-launcher install|start|stop|remove        # (Windows) kelola service, diteruskan ke pywin32
"""
import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time
from pathlib import Path

from ronda_agent.config import default_data_dir
from ronda_agent.release import Layout
from ronda_launcher.core import Launcher
from ronda_launcher.helper import make_supervisor


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime


def setup_logging(data_dir: Path, console: bool) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = _UtcFormatter("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        log_dir / "launcher.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if sys.platform == "win32" and (not argv or argv[0] != "console"):
        from ronda_launcher import service
        return service.main(argv)
    parser = argparse.ArgumentParser(prog="ronda-launcher")
    parser.add_argument("mode", choices=["console"])
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args(argv)
    setup_logging(args.data_dir, console=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    data_dir = args.data_dir.resolve()
    Launcher(data_dir, helper=make_supervisor(Layout(data_dir))).run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
