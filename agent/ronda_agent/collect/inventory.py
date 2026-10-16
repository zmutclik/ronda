"""Spesifikasi PC, disk, dan adaptor jaringan untuk PUT /api/agent/inventory.

Fungsi `build_*` / `parse_*` murni (dites di Linux); sumber Windows (WMI, registry, netsh) dibaca di `collect()`.
Kegagalan satu sumber tidak menggagalkan seluruh inventaris: field-nya dikirim kosong.
"""
import ipaddress
import logging
import platform
import re
import socket
import subprocess
import sys
import time

import psutil

from ronda_agent import schemas

log = logging.getLogger(__name__)

ADAPTERS_MAX = 32
_VIRTUAL_RE = re.compile(r"hyper-v|virtual|vmware|virtualbox|vbox|tap-|tap |wintun|wireguard|loopback|miniport|"
                         r"vethernet|docker|npcap|teredo|isatap|pseudo|fortinet|cisco anyconnect|openvpn|^br-|^veth|^virbr", re.I)
_JUNK_SERIALS = {"", "0", "none", "default string", "to be filled by o.e.m.", "system serial number",
                 "not specified", "not applicable", "chassis serial number", "123456789"}
_MEDIA = {3: "HDD", 4: "SSD", 5: "SCM"}


# ---------- fungsi murni ----------

def clean_serial(value) -> str | None:
    text = schemas.text(value, schemas.SERIAL_MAX)
    return None if text is None or text.lower() in _JUNK_SERIALS else text


def windows_os_name(product: str | None, display_version: str | None, build: str | None) -> str | None:
    """Registry Windows 11 tetap menulis ProductName "Windows 10 …"; build ≥ 22000 = Windows 11."""
    if not product:
        return None
    try:
        if build and int(build) >= 22000:
            product = product.replace("Windows 10", "Windows 11")
    except ValueError:
        pass
    return schemas.text(f"{product} {display_version or ''}", 60)


def parse_netsh_wlan(text: str) -> dict[str, dict]:
    """`netsh wlan show interfaces` → {nama adaptor: {ssid, signal_pct}}. Label bisa berbahasa Indonesia/Inggris,
    jadi yang dipakai: field pertama tiap blok = nama, label tepat "SSID", dan satu-satunya nilai berakhiran %."""
    result: dict[str, dict] = {}
    for block in re.split(r"\n\s*\n", text):
        pairs = [(k.strip(), v.strip()) for k, sep, v in (line.partition(":") for line in block.splitlines())
                 if sep and k.strip()]
        if len(pairs) < 3:
            continue
        name = pairs[0][1]
        info = {"ssid": None, "signal_pct": None}
        for key, value in pairs:
            if key == "SSID":
                info["ssid"] = schemas.text(value, 64)
            elif re.fullmatch(r"\d{1,3}\s*%", value):
                info["signal_pct"] = min(100, int(value.rstrip("% ")))
        result[name] = info
    return result


def _prefix(netmask: str | None) -> int | None:
    if not netmask:
        return None
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except ValueError:
        return None


def build_adapters(addrs: dict, stats: dict, io: dict, *, wmi: dict[str, dict] | None = None,
                   wlan: dict[str, dict] | None = None, route_ip: str | None = None,
                   prev_io: dict | None = None, seconds: float | None = None) -> list[dict]:
    """Gabungkan psutil (alamat, status, trafik) dengan WMI (deskripsi, gateway, DNS, DHCP) dan netsh (Wi-Fi).
    `wmi` dikunci nama koneksi ("Ethernet", "Wi-Fi") — sama dengan nama di psutil Windows."""
    wmi, wlan, prev_io = wmi or {}, wlan or {}, prev_io or {}
    adapters = []
    for name, entries in addrs.items():
        mac = ipv4 = netmask = None
        for e in entries:
            if e.family == psutil.AF_LINK:
                mac = e.address.replace("-", ":").upper()
            elif e.family == socket.AF_INET and ipv4 is None:
                ipv4, netmask = e.address, e.netmask
        if ipv4 and ipv4.startswith("127."):
            continue                               # loopback
        st, w, wifi = stats.get(name), wmi.get(name, {}), wlan.get(name)
        description = w.get("description")
        is_virtual = (w.get("physical") is False
                      or bool(_VIRTUAL_RE.search(f"{name} {description or ''}")))
        rx = tx = None
        if name in io and name in prev_io and seconds:
            rx = max(0, int((io[name].bytes_recv - prev_io[name].bytes_recv) * 8 / seconds))
            tx = max(0, int((io[name].bytes_sent - prev_io[name].bytes_sent) * 8 / seconds))
        adapters.append({
            "name": schemas.text(name, 80) or "?", "description": schemas.text(description, 120),
            "mac": mac if mac and len(mac) == 17 else None,
            "is_virtual": is_virtual, "is_up": bool(st and st.isup),
            "is_server_route": bool(route_ip and ipv4 == route_ip),
            "speed_mbps": (st.speed or None) if st else None,
            "ssid": wifi.get("ssid") if wifi else None, "signal_pct": wifi.get("signal_pct") if wifi else None,
            "ipv4": ipv4, "prefix_len": _prefix(netmask),
            "gateway": schemas.text(w.get("gateway"), 45), "dns": schemas.text(w.get("dns"), 200),
            "dhcp": w.get("dhcp"), "rx_bps": rx, "tx_bps": tx,
        })
    # adaptor rute ke server dulu, lalu fisik yang aktif; batas server 32
    adapters.sort(key=lambda a: (not a["is_server_route"], a["is_virtual"], not a["is_up"], a["name"]))
    return adapters[:ADAPTERS_MAX]


def build_disks(usage: list[dict], labels: dict[str, str] | None = None, media: dict[str, str] | None = None) -> list[dict]:
    """usage: [{"mount", "total_bytes", "used_bytes"}] dari psutil; label/media per mount dari WMI."""
    labels, media = labels or {}, media or {}
    return [{"mount": d["mount"], "label": schemas.text(labels.get(d["mount"]), 40),
             "media": schemas.text(media.get(d["mount"]), 10), "total_bytes": d["total_bytes"],
             "used_bytes": d["used_bytes"]} for d in usage][:schemas.DISKS_MAX]


# ---------- sumber data ----------

def _wmi(query: str, namespace: str = r"root\cimv2") -> list:
    import win32com.client
    return list(win32com.client.GetObject(f"winmgmts:{namespace}").ExecQuery(query))


def _safe(label: str, fn, default=None):
    try:
        return fn()
    except Exception as err:          # WMI/registry/netsh bisa gagal sebagian; inventaris tetap dikirim
        log.warning("inventaris: %s gagal: %s", label, err)
        return default


def _disk_usage() -> list[dict]:
    from ronda_agent.collect.metrics import fixed_partitions
    result = []
    for mount, path in fixed_partitions():
        try:
            u = psutil.disk_usage(path)
        except OSError:
            continue
        result.append({"mount": mount, "total_bytes": u.total, "used_bytes": u.used})
    return result


def _windows_os() -> dict:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
        def get(name):
            try:
                return winreg.QueryValueEx(key, name)[0]
            except OSError:
                return None
        build, ubr = get("CurrentBuild"), get("UBR")
        return {"os_name": windows_os_name(get("ProductName"), get("DisplayVersion"), build),
                "os_build": schemas.text(f"{build}.{ubr}" if ubr is not None else build, 30)}


def _windows_hardware() -> dict:
    cs = _wmi("SELECT Manufacturer, Model FROM Win32_ComputerSystem")[0]
    cpus = _wmi("SELECT Name, NumberOfCores, NumberOfLogicalProcessors FROM Win32_Processor")
    bios = _wmi("SELECT SerialNumber FROM Win32_BIOS")
    return {"manufacturer": schemas.text(cs.Manufacturer, 60), "model": schemas.text(cs.Model, 80),
            "cpu_model": schemas.text(" ".join(str(cpus[0].Name or "").split()), 100) if cpus else None,
            "cpu_cores": sum(int(c.NumberOfCores or 0) for c in cpus) or None,
            "cpu_threads": sum(int(c.NumberOfLogicalProcessors or 0) for c in cpus) or None,
            "serial_number": clean_serial(bios[0].SerialNumber) if bios else None}


def _windows_disk_details() -> tuple[dict, dict]:
    labels = {d.DeviceID: d.VolumeName for d in _wmi("SELECT DeviceID, VolumeName FROM Win32_LogicalDisk")}
    storage = r"root\Microsoft\Windows\Storage"
    disk_media = {str(d.DeviceId): _MEDIA.get(int(d.MediaType or 0))
                  for d in _wmi("SELECT DeviceId, MediaType FROM MSFT_PhysicalDisk", storage)}
    media = {}
    for p in _wmi("SELECT DriveLetter, DiskNumber FROM MSFT_Partition", storage):
        letter = str(p.DriveLetter or "").strip("\x00 ")
        if letter and disk_media.get(str(p.DiskNumber)):
            media[f"{letter}:"] = disk_media[str(p.DiskNumber)]
    return labels, media


def _windows_adapter_details() -> dict[str, dict]:
    configs = {int(c.Index): c for c in _wmi(
        "SELECT Index, DefaultIPGateway, DNSServerSearchOrder, DHCPEnabled FROM Win32_NetworkAdapterConfiguration")}
    result = {}
    for a in _wmi("SELECT NetConnectionID, Name, PhysicalAdapter, Index FROM Win32_NetworkAdapter "
                  "WHERE NetConnectionID IS NOT NULL"):
        c = configs.get(int(a.Index))
        gateways = [g for g in (c.DefaultIPGateway or ()) if ":" not in g] if c else []
        result[a.NetConnectionID] = {
            "description": a.Name, "physical": bool(a.PhysicalAdapter),
            "gateway": gateways[0] if gateways else None,
            "dns": (", ".join(d for d in (c.DNSServerSearchOrder or ()) if ":" not in d) or None) if c else None,
            "dhcp": bool(c.DHCPEnabled) if c else None}
    return result


def _netsh_wlan() -> dict[str, dict]:
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, timeout=10,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return parse_netsh_wlan(out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else "")


def _linux_cpu_model() -> str | None:
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


class InventoryCollector:
    """Menyimpan counter trafik antar-pengumpulan agar rx/tx per adaptor bisa dihitung."""

    def __init__(self):
        self._prev_io: tuple[float, dict] | None = None

    def serial_number(self) -> str | None:
        if sys.platform != "win32":
            return None
        return _safe("serial", lambda: clean_serial(_wmi("SELECT SerialNumber FROM Win32_BIOS")[0].SerialNumber))

    def collect(self, route_ip: str | None) -> dict:
        windows = sys.platform == "win32"
        data: dict = {"ram_total_mb": psutil.virtual_memory().total // (1024 * 1024)}
        if windows:
            data |= _safe("OS", _windows_os, {})
            data |= _safe("hardware", _windows_hardware, {})
            labels, media = _safe("disk", _windows_disk_details, ({}, {}))
            wmi_adapters = _safe("adaptor", _windows_adapter_details, {})
            wlan = _safe("netsh wlan", _netsh_wlan, {})
        else:
            data |= {"os_name": schemas.text(f"{platform.system()} {platform.release()}", 60),
                     "os_build": schemas.text(platform.version(), 30), "cpu_model": schemas.text(_linux_cpu_model(), 100),
                     "cpu_cores": psutil.cpu_count(logical=False), "cpu_threads": psutil.cpu_count()}
            labels, media, wmi_adapters, wlan = {}, {}, {}, {}
        data.setdefault("cpu_threads", psutil.cpu_count())
        data["disks"] = build_disks(_disk_usage(), labels, media)

        now, io = time.monotonic(), psutil.net_io_counters(pernic=True)
        prev_t, prev_io = self._prev_io or (None, None)
        self._prev_io = (now, io)
        data["adapters"] = build_adapters(psutil.net_if_addrs(), psutil.net_if_stats(), io, wmi=wmi_adapters,
                                          wlan=wlan, route_ip=route_ip, prev_io=prev_io,
                                          seconds=(now - prev_t) if prev_t else None)
        return {k: v for k, v in data.items() if v is not None}
