"""Loop utama agent: enroll, heartbeat, sampler metrik + disk, batch metrics, pemulihan 401.

Satu thread, satu jadwal: tiap tugas punya waktu jatuh tempo (monotonic). `tick()` menjalankan yang sudah jatuh
tempo; test memanggilnya langsung dengan jam palsu.
"""
import argparse
import logging
import logging.handlers
import random
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ronda_agent import __version__, schemas
from ronda_agent.api import Api, ApiError, Forbidden, Invalid, Transient, Unauthorized, make_http_client
from ronda_agent.backoff import Backoff
from ronda_agent.buffer import Buffer
from ronda_agent.collect.apps import apps_hash
from ronda_agent.commands import ScreenshotJob
from ronda_agent.config import Config, ConfigError, State, StateStore, default_data_dir, load_config, \
    remove_enrollment_key
from ronda_agent.pipe import HelperClient
from ronda_agent.remote import RemoteJob, UltraVnc, default_uvnc_dir, open_relay
from ronda_agent.release import UPDATE_EXIT_CODE, Layout, ReleaseError, version_key
from ronda_agent.schemas import iso, utc_now
from ronda_agent.updater import UpdateJob

log = logging.getLogger("ronda_agent")

RECHECK_SEC = 3600          # 401 / key ditolak: cek config tiap 1 jam
PRUNE_SEC = 3600
COMMAND_MEMORY_SEC = 15 * 60  # server mengulang perintah screenshot maks 10 menit
MAX_BATCHES_PER_FLUSH = 20
UPDATE_RETRY_SEC = 600        # cek update gagal karena jaringan: coba lagi 10 menit
INVENTORY_RETRY_SEC = 600
NEVER = float("inf")


class SystemCollectors:
    """Sumber data sungguhan (psutil). Test memakai pengganti dengan method yang sama."""

    def __init__(self, server_url: str):
        from ronda_agent.collect import apps, inventory, metrics
        self._m, self._apps = metrics, apps
        self._sampler = metrics.Sampler(server_url)
        self._inventory = inventory.InventoryCollector()

    def sample(self, ts, latency_ms):
        return self._sampler.sample(ts, latency_ms)

    def probe(self):
        return self._sampler.probe()

    def disk_usage(self):
        return self._m.disk_usage()

    def logged_user(self):
        return self._m.logged_user()

    def boot_time(self):
        return self._m.boot_time()

    def hostname(self) -> str:
        return socket.gethostname()

    def serial_number(self) -> str | None:
        return self._inventory.serial_number()

    def inventory(self, route_ip: str | None) -> dict:
        return self._inventory.collect(route_ip)

    def apps(self) -> list[dict]:
        return self._apps.collect()


class Agent:
    def __init__(self, data_dir: Path, api: Api, collectors, buffer: Buffer, store: StateStore,
                 config_path: Path | None = None, clock=time.monotonic, now=utc_now, rng: random.Random | None = None,
                 helper: HelperClient | None = None, vnc: UltraVnc | None = None,
                 relay_connect: Callable[[str], object] | None = None):
        self.data_dir = data_dir
        self.config_path = config_path or data_dir / "config.toml"
        self.cfg: Config = load_config(self.config_path)
        self.api, self.collect, self.buffer, self.store = api, collectors, buffer, store
        self.clock, self.now = clock, now
        self.rng = rng or random.Random()
        self.state: State = store.load()
        self.api.token = self.state.agent_token
        self.phase = "pending" if self.state.agent_token else "enroll"   # pending → online setelah heartbeat
        self.layout = Layout(data_dir)
        launcher = self.layout.read_launcher_state()     # hasil update terakhir dicatat launcher
        self.update_status = launcher.get("update_status") if launcher.get("update_status") in (
            "installed", "rollback") else "none"
        self.bad_versions: set[str] = set(launcher.get("bad_versions") or [])
        self.update_offer: dict | None = None
        self.update_job: UpdateJob | None = None
        self.release_keys: tuple[str, ...] | None = None     # None = kunci tertanam (release_keys.py)
        self.exit_code: int | None = None
        self.boot_at = iso(collectors.boot_time())
        self.latency_ms: float | None = None
        self.ip: str | None = None
        self.last_sample: dict | None = None
        self.last_disks: list[dict] = []
        self.healthy_written = False
        self.server_down = False
        self.handled_commands: dict[str, float] = {}
        self.helper = helper
        self.screenshot_job: ScreenshotJob | None = None
        self.vnc = vnc
        self.relay_connect = relay_connect or (
            lambda path: open_relay(self.cfg.server_url, self.cfg.ca_bundle, self.api.token or "", path))
        self.remote_job: RemoteJob | None = None
        self.enroll_backoff = Backoff(rng=self.rng)
        self.metrics_backoff = Backoff(rng=self.rng)
        self.due: dict[str, float] = dict.fromkeys(("latency", "disk", "sample", "enroll", "heartbeat", "metrics",
                                                    "recheck", "prune", "update_check", "inventory"), clock())
        self.force_apps = False
        self.due["update_start"] = NEVER

    # ---------- jadwal ----------

    def _is_due(self, task: str) -> bool:
        return self.clock() >= self.due[task]

    def _later(self, task: str, seconds: float) -> None:
        self.due[task] = self.clock() + seconds

    def seconds_to_next(self) -> float:
        return max(0.0, min(self.due.values()) - self.clock())

    def tick(self) -> None:
        if self.phase == "revoked":
            if self._is_due("recheck"):
                self._recheck_config()
            if self.phase == "revoked":
                return
        if self._is_due("latency"):
            self._later("latency", self.cfg.latency_sec)
            self.latency_ms, ip = self.collect.probe()
            self.ip = ip or self.ip
        if self._is_due("disk"):
            self._later("disk", self.cfg.disk_sec)
            self._collect_disks()
        if self._is_due("sample"):
            self._later("sample", self.cfg.sample_sec)
            self.last_sample = self.collect.sample(self.now(), self.latency_ms)
            self.buffer.add_sample(self.last_sample)
        if self._is_due("prune"):
            self._later("prune", PRUNE_SEC)
            if dropped := self.buffer.prune(self.now()):
                log.warning("buffer penuh/kedaluwarsa: %d baris tertua dibuang", dropped)

        if self.phase == "enroll":
            if self._is_due("enroll"):
                self._enroll()
            if self.phase == "enroll":
                return
        if self.update_job and self.update_job.result:    # sebelum heartbeat: status terbaru langsung terlapor
            self._finish_update()
        if self.screenshot_job and self.screenshot_job.result:
            if self.screenshot_job.result == "retry":       # server mengulang perintah; proses lagi nanti
                self.handled_commands.pop(self.screenshot_job.request_id, None)
            self.screenshot_job = None
        if self.remote_job and self.remote_job.result:
            self.remote_job = None
        if self._is_due("heartbeat"):
            self._later("heartbeat", self.cfg.heartbeat_sec)
            self._heartbeat()
        if self.phase == "online" and self._is_due("inventory"):
            self._send_inventory()
        if self.phase == "online" and self._is_due("metrics"):
            self._flush_metrics()
        if self.phase == "online" and self._is_due("update_check"):
            self._check_update()
        if self._is_due("update_start"):
            self._start_update()

    # ---------- pengumpulan ----------

    def _collect_disks(self) -> None:
        ts = iso(self.now())
        self.last_disks = self.collect.disk_usage()
        if self.last_disks:
            self.buffer.add_disks([{"ts": ts, **d} for d in self.last_disks])

    # ---------- enroll ----------

    def _enroll(self) -> None:
        if not self.cfg.enrollment_key:
            log.error("belum terdaftar dan enrollment_key kosong di %s; cek ulang 1 jam lagi", self.config_path)
            self._later("enroll", RECHECK_SEC)
            self._reload_config()
            return
        payload = {"enrollment_key": self.cfg.enrollment_key,
                   "hostname": schemas.text(self.collect.hostname(), schemas.HOSTNAME_MAX) or "unknown",
                   "agent_version": __version__, "ip": self.ip,
                   "serial_number": schemas.text(self.collect.serial_number(), schemas.SERIAL_MAX)}
        try:
            result = self.api.enroll(payload)
        except Transient as err:
            delay = self.enroll_backoff.next_delay()
            log.warning("enroll gagal (%s); ulang dalam %.0f dtk", err, delay)
            self._later("enroll", delay)
            return
        except ApiError as err:          # 403 key tidak valid, 422 payload
            log.error("enroll ditolak: %s; cek config lagi 1 jam", err)
            self._later("enroll", RECHECK_SEC)
            self._reload_config()
            return
        self.enroll_backoff.reset()
        self.state = State(agent_token=result["agent_token"], host_id=result["host_id"], extra=self.state.extra)
        self.store.save(self.state)
        self.api.token = self.state.agent_token
        remove_enrollment_key(self.config_path)
        self.cfg.enrollment_key = None
        self.phase = "online" if result.get("status") == "online" else "pending"
        log.info("terdaftar sebagai host %s, status %s", result["host_id"], result.get("status"))
        self._later("heartbeat", 0)
        self._later("metrics", 0)

    def _reload_config(self) -> None:
        try:
            self.cfg = load_config(self.config_path)
        except ConfigError as err:
            log.error("config: %s", err)

    def _recheck_config(self) -> None:
        self._later("recheck", RECHECK_SEC)
        self._reload_config()
        if self.cfg.enrollment_key:
            log.info("enrollment key baru ditemukan; mendaftar ulang")
            self.phase = "enroll"
            self._later("enroll", 0)

    def _revoke(self, err: ApiError) -> None:
        log.error("token agent ditolak server (%s); berhenti kirim data, cek config tiap 1 jam", err)
        self.phase = "revoked"
        self._later("recheck", 0)

    # ---------- heartbeat ----------

    def _heartbeat(self) -> None:
        sample = self.last_sample or self.collect.sample(self.now(), self.latency_ms)
        payload = {"ts": iso(self.now()), "agent_version": __version__, "update_status": self.update_status,
                   "ip": self.ip, "cpu_pct": sample["cpu_pct"], "ram_pct": sample["ram_pct"],
                   "disks": self.last_disks, "latency_ms": self.latency_ms,
                   "logged_user": self.collect.logged_user(), "boot_at": self.boot_at}
        try:
            result = self.api.heartbeat(payload)
        except Unauthorized as err:
            self._revoke(err)
            return
        except Transient as err:
            if not self.server_down:
                log.warning("server tidak terjangkau: %s", err)
            self.server_down = True
            return
        except ApiError as err:
            log.error("heartbeat ditolak: %s", err)
            return
        if self.server_down:
            log.info("server terjangkau lagi")
            self.server_down = False
            self.metrics_backoff.reset()
            self._later("metrics", 0)       # kosongkan buffer sekarang, jangan tunggu sisa backoff
        status = result.get("status")
        if status == "online" and self.phase != "online":
            log.info("PC disetujui server; mulai kirim metrik")
            self._later("metrics", 0)
        self.phase = "online" if status == "online" else "pending"
        if not self.healthy_written:
            (self.data_dir / "healthy").write_text(f"{__version__} {iso(self.now())}\n")
            self.healthy_written = True
        for command in result.get("commands") or []:
            self._command(command)

    # ---------- perintah ----------

    def _command(self, command: dict) -> None:
        kind = command.get("type")
        now = self.clock()
        self.handled_commands = {k: t for k, t in self.handled_commands.items() if t > now}
        if kind == "screenshot":
            request_id = str(command.get("request_id") or "")
            if not request_id or request_id in self.handled_commands or self.screenshot_job:
                return                  # satu per satu; server mengulang perintah yang belum selesai
            self.handled_commands[request_id] = now + COMMAND_MEMORY_SEC
            log.info("screenshot %s diminta server", request_id)
            notify = command.get("notify") is not False     # server lama tanpa field: tetap beri tahu user
            self.screenshot_job = ScreenshotJob(self.api, self.helper if self.cfg.screenshot else None, request_id,
                                                self.collect.logged_user, notify=notify).start()
        elif kind == "remote":
            self._remote(command, now)
        elif kind == "refresh_inventory":
            log.info("refresh_inventory diminta server")
            self.force_apps = True        # kirim ulang daftar aplikasi walau hash sama (server mengabaikan bila sama)
            self._later("inventory", 0)
        else:
            log.warning("perintah tidak dikenal diabaikan: %r", kind)

    def _remote(self, command: dict, now: float) -> None:
        try:
            session_id = int(command.get("session_id"))
        except (TypeError, ValueError):
            log.warning("perintah remote tanpa session_id diabaikan")
            return
        key = f"remote-{session_id}"
        if key in self.handled_commands or self.remote_job:
            return                      # satu sesi per PC; server mengulang perintah sampai tersambung/kedaluwarsa
        self.handled_commands[key] = now + COMMAND_MEMORY_SEC
        mode = "control" if command.get("mode") == "control" else "view"
        log.info("remote %s (%s) diminta server oleh %s", session_id, mode, command.get("user"))
        helper = self.helper
        self.remote_job = RemoteJob(
            self.api, session_id, mode=mode, user=str(command.get("user") or "admin"),
            relay_path=str(command.get("relay_path") or ""), vnc=self.vnc, connect=self.relay_connect,
            notify=helper.notify if helper else None, enabled=self.cfg.remote).start()

    # ---------- inventaris & aplikasi ----------

    def _send_inventory(self) -> None:
        """Spesifikasi + disk + adaptor selalu dikirim; aplikasi hanya bila hash berubah (atau diminta server)."""
        self._later("inventory", self.cfg.inventory_hours * 3600)
        try:
            payload = self.collect.inventory(self.ip)
            apps = self.collect.apps()
        except Exception:
            log.exception("pengumpulan inventaris gagal")
            return
        digest = apps_hash(apps)
        try:
            self.api.inventory(payload)
            if self.force_apps or digest != self.state.apps_hash:
                changed = self.api.apps(digest, apps).get("changed")
                log.info("daftar aplikasi dikirim (%d aplikasi, berubah: %s)", len(apps), changed)
                self.state.apps_hash = digest
                self.store.save(self.state)
            self.force_apps = False
        except Unauthorized as err:
            self._revoke(err)
        except Forbidden:
            self.phase = "pending"
            self._later("inventory", 0)          # kirim begitu disetujui
        except Transient as err:
            log.warning("kirim inventaris gagal (%s); ulang 10 menit", err)
            self._later("inventory", INVENTORY_RETRY_SEC)
        except ApiError as err:                  # 422: tunggu jadwal berikutnya, jangan diulang terus
            log.error("inventaris ditolak server: %s", err)

    # ---------- auto-update ----------

    def _check_update(self) -> None:
        self._later("update_check", self.cfg.update_hours * 3600)
        if self.update_offer or self.update_job:
            return
        try:
            offer = self.api.update_check()
        except Unauthorized as err:
            self._revoke(err)
            return
        except Forbidden:
            self.phase = "pending"
            return
        except ApiError as err:
            log.warning("cek update gagal: %s", err)
            self._later("update_check", UPDATE_RETRY_SEC)
            return
        if not offer:
            return
        version = str(offer.get("version"))
        try:
            newer = version_key(version) > version_key(__version__)
        except ReleaseError:
            newer = False
        if not newer:
            # Tanda tangan hanya menjamin paket asli, bukan lebih baru: server yang dibobol bisa menawarkan rilis
            # lama yang sah (dan punya celah). Downgrade sengaja tidak didukung lewat auto-update.
            log.warning("tawaran rilis %r ditolak: tidak lebih baru dari %s", version, __version__)
            return
        if version in self.bad_versions:
            log.info("rilis %s pernah di-rollback di PC ini; dilewati", version)
            return
        delay = self.rng.uniform(0, self.cfg.update_jitter_sec)
        log.info("rilis %s tersedia; mulai unduh dalam %.0f dtk", version, delay)
        self.update_offer, self.update_status = offer, "waiting"
        self._later("update_start", delay)

    def _start_update(self) -> None:
        self.due["update_start"] = NEVER
        if not self.update_offer:
            return
        self.update_status = "downloading"
        self.update_job = UpdateJob(self.api, self.layout, self.update_offer, self.release_keys).start()

    def _finish_update(self) -> None:
        job, self.update_job, self.update_offer = self.update_job, None, None
        if job.result == "ready":
            log.info("keluar untuk pindah ke versi %s", job.version)
            self.exit_code = UPDATE_EXIT_CODE
        else:
            self.update_status = "failed"

    # ---------- metrics ----------

    def _flush_metrics(self) -> None:
        for _ in range(MAX_BATCHES_PER_FLUSH):
            samples, disks = self.buffer.peek(schemas.BATCH_MAX)
            if not samples and not disks:
                break
            try:
                self.api.metrics(samples, disks)
            except Transient as err:
                delay = self.metrics_backoff.next_delay()
                log.warning("kirim metrik gagal (%s); ulang dalam %.0f dtk", err, delay)
                self._later("metrics", delay)
                return
            except Unauthorized as err:
                self._revoke(err)
                return
            except Forbidden:
                self.phase = "pending"           # tetap di buffer; heartbeat jalan terus
                break
            except Invalid as err:
                log.error("batch metrik ditolak, dibuang (%d sampel, %d disk): %s", len(samples), len(disks), err)
            except ApiError as err:
                log.error("batch metrik gagal: %s; ulang di jadwal berikutnya", err)
                break
            self.buffer.remove(samples, disks)
            self.metrics_backoff.reset()
        self._later("metrics", self.cfg.metrics_batch_sec)

    # ---------- run ----------

    def run(self, stop: threading.Event) -> int:
        log.info("ronda-agent %s mulai (server %s)", __version__, self.cfg.server_url)
        while not stop.is_set() and self.exit_code is None:
            try:
                self.tick()
            except Exception:
                log.exception("galat tak terduga di loop utama")
                time.sleep(5)
            stop.wait(min(5.0, max(0.2, self.seconds_to_next())))
        log.info("ronda-agent berhenti")
        return self.exit_code or 0


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime


def setup_logging(log_dir: Path, console: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = _UtcFormatter("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        log_dir / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)    # httpx mencatat tiap request di INFO


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ronda-agent")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--console", action="store_true", help="log juga ke stderr")
    args = parser.parse_args(argv)
    data_dir: Path = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir / "logs", args.console)
    try:
        cfg = load_config(data_dir / "config.toml")
    except ConfigError as err:
        log.error("config: %s", err)
        return 2

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    buffer = Buffer(data_dir / "buffer.db")
    uvnc_dir = Path(cfg.uvnc_dir) if cfg.uvnc_dir else default_uvnc_dir()
    vnc = UltraVnc(uvnc_dir) if uvnc_dir else None
    if vnc and not vnc.installed:
        log.warning("UltraVNC tidak ada di %s; perintah remote akan dilaporkan gagal", uvnc_dir)
        vnc = None
    if vnc:
        vnc.stop()                  # sisa sesi bila agent sebelumnya berhenti di tengah sesi remote
    with make_http_client(cfg.server_url, cfg.ca_bundle) as http:
        agent = Agent(data_dir, Api(http), SystemCollectors(cfg.server_url), buffer, StateStore(data_dir / "state.dat"),
                      helper=HelperClient(data_dir), vnc=vnc)
        code = agent.run(stop)
    buffer.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
