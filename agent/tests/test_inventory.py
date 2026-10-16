"""Tahap 3: normalisasi aplikasi + hash, pembentuk inventaris (data mentah Windows tiruan), dan alur kirim di agent."""
import socket
import sys
from collections import namedtuple

import psutil
import pytest

from ronda_agent.collect import apps, inventory
from ronda_agent.config import StateStore
from tests.fakes import FakeCollectors, run_for

Addr = namedtuple("Addr", "family address netmask broadcast ptp")
Stat = namedtuple("Stat", "isup duplex speed mtu flags")
Io = namedtuple("Io", "bytes_sent bytes_recv")


# ---------- aplikasi ----------

RAW = [
    {"DisplayName": "Google Chrome", "DisplayVersion": "129.0.6668", "Publisher": "Google LLC", "InstallDate": "20260901"},
    {"DisplayName": "Google Chrome", "DisplayVersion": "129.0.6668", "Publisher": "Google LLC"},     # HKU duplikat
    {"DisplayName": "SIMRS Client", "DisplayVersion": "4.2.1", "Publisher": "RS Contoh", "InstallDate": "2026"},
    {"DisplayName": "7-Zip 23.01 (x64)", "DisplayVersion": 2301, "InstallDate": "20261345"},          # tanggal tak valid
    {"DisplayName": "Microsoft Visual C++ Runtime", "SystemComponent": 1},
    {"DisplayName": "Security Update for Office (KB5002)", "ParentKeyName": "Office16.STANDARD"},
    {"DisplayVersion": "1.0"},                                                                         # tanpa nama
    {"DisplayName": "   "},
    {"DisplayName": "X" * 300, "DisplayVersion": "v" * 80, "Publisher": "P" * 200},
]


def test_normalize_filters_dedupes_sorts_and_truncates():
    result = apps.normalize(RAW)
    assert [a["name"][:20] for a in result] == ["7-Zip 23.01 (x64)", "Google Chrome", "SIMRS Client", "X" * 20]
    chrome = result[1]
    assert chrome == {"name": "Google Chrome", "version": "129.0.6668", "publisher": "Google LLC",
                      "install_date": "2026-09-01"}
    assert result[0]["version"] == "2301" and result[0]["install_date"] is None
    assert result[2]["install_date"] is None
    long = result[3]
    assert len(long["name"]) == 255 and len(long["version"]) == 60 and len(long["publisher"]) == 150


def test_hash_ignores_registry_order_and_matches_spec():
    a = apps.normalize(RAW)
    b = apps.normalize(list(reversed(RAW)))
    assert a == b and apps.apps_hash(a) == apps.apps_hash(b)
    assert len(apps.apps_hash(a)) == 64
    assert apps.apps_hash(a) != apps.apps_hash(a[:-1])
    import hashlib
    import json
    assert apps.apps_hash(a) == hashlib.sha256(
        json.dumps(a, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_normalize_caps_at_server_limit():
    many = [{"DisplayName": f"App {i:05d}"} for i in range(2500)]
    assert len(apps.normalize(many)) == 2000


# ---------- inventaris ----------

def test_windows_os_name_fixes_windows_11_product_name():
    assert inventory.windows_os_name("Windows 10 Pro", "23H2", "22631") == "Windows 11 Pro 23H2"
    assert inventory.windows_os_name("Windows 10 Pro", "22H2", "19045") == "Windows 10 Pro 22H2"
    assert inventory.windows_os_name(None, "22H2", "19045") is None


@pytest.mark.parametrize(("raw", "expected"), [("PF2ABC12", "PF2ABC12"), ("To be filled by O.E.M.", None),
                                                ("Default string", None), ("  ", None), (None, None)])
def test_clean_serial(raw, expected):
    assert inventory.clean_serial(raw) == expected


NETSH_EN = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 4b1c3f1e-0000-0000-0000-000000000000
    State                  : connected
    SSID                   : RS-Staff
    BSSID                  : a4:c3:f0:5d:21:9b
    Signal                 : 72%
    Profile                : RS-Staff
"""
NETSH_ID = """
Ada 1 antarmuka di sistem:

    Nama                   : Wi-Fi 2
    Deskripsi              : Realtek RTL8821CE
    Status                 : tersambung
    SSID                   : RS-Tamu
    BSSID                  : 00:11:22:33:44:55
    Sinyal                 : 38%
"""


def test_parse_netsh_english_and_indonesian():
    assert inventory.parse_netsh_wlan(NETSH_EN) == {"Wi-Fi": {"ssid": "RS-Staff", "signal_pct": 72}}
    assert inventory.parse_netsh_wlan(NETSH_ID) == {"Wi-Fi 2": {"ssid": "RS-Tamu", "signal_pct": 38}}
    assert inventory.parse_netsh_wlan("Wireless AutoConfig Service (wlansvc) is not running.") == {}


def test_build_adapters_merges_sources():
    addrs = {
        "Ethernet": [Addr(psutil.AF_LINK, "3C-52-82-1A-7F-0E", None, None, None),
                     Addr(socket.AF_INET, "10.10.12.31", "255.255.255.0", None, None)],
        "Wi-Fi": [Addr(psutil.AF_LINK, "A4-C3-F0-5D-21-9B", None, None, None),
                  Addr(socket.AF_INET, "192.168.50.118", "255.255.255.0", None, None)],
        "vEthernet (Default Switch)": [Addr(socket.AF_INET, "172.24.80.1", "255.255.240.0", None, None)],
        "Loopback Pseudo-Interface 1": [Addr(socket.AF_INET, "127.0.0.1", "255.0.0.0", None, None)],
    }
    stats = {"Ethernet": Stat(True, 2, 1000, 1500, ""), "Wi-Fi": Stat(False, 0, 0, 1500, ""),
             "vEthernet (Default Switch)": Stat(True, 2, 10000, 1500, "")}
    prev = {"Ethernet": Io(1_000_000, 2_000_000)}
    io = {"Ethernet": Io(1_500_000, 3_500_000)}
    wmi = {"Ethernet": {"description": "Realtek PCIe GbE Family Controller", "physical": True,
                        "gateway": "10.10.12.1", "dns": "10.10.1.10, 10.10.1.11", "dhcp": True},
           "vEthernet (Default Switch)": {"description": "Hyper-V Virtual Ethernet Adapter", "physical": False}}
    wlan = inventory.parse_netsh_wlan(NETSH_EN)
    result = inventory.build_adapters(addrs, stats, io, wmi=wmi, wlan=wlan, route_ip="10.10.12.31",
                                      prev_io=prev, seconds=10)
    assert [a["name"] for a in result] == ["Ethernet", "Wi-Fi", "vEthernet (Default Switch)"]   # loopback dibuang
    eth, wifi, virt = result
    assert eth["is_server_route"] and eth["is_up"] and not eth["is_virtual"]
    assert eth["mac"] == "3C:52:82:1A:7F:0E" and eth["prefix_len"] == 24 and eth["speed_mbps"] == 1000
    assert eth["gateway"] == "10.10.12.1" and eth["dhcp"] is True
    assert eth["rx_bps"] == 1_200_000 and eth["tx_bps"] == 400_000
    assert wifi["ssid"] == "RS-Staff" and wifi["signal_pct"] == 72 and not wifi["is_up"]
    assert wifi["speed_mbps"] is None and wifi["rx_bps"] is None
    assert virt["is_virtual"] and virt["prefix_len"] == 20


# ---------- alur agent ----------

def _sent(server, path):
    return [b for p, b in server.calls if p == path]


def test_inventory_and_apps_sent_when_online_then_only_on_change(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    agent.tick()
    paths = server.paths()
    assert paths.index("/api/agent/inventory") < paths.index("/api/agent/apps") < paths.index("/api/agent/metrics")
    assert _sent(server, "/api/agent/inventory")[0]["adapters"][0]["ipv4"] == "10.10.12.31"
    first = _sent(server, "/api/agent/apps")[0]
    assert first["apps"] == FakeCollectors.apps_list and len(first["hash"]) == 64
    assert StateStore(data_dir / "state.dat").load().apps_hash == first["hash"]

    run_for(agent, clock, 24 * 3600 + 60, step=60)          # 24 jam kemudian: inventaris lagi, aplikasi tidak
    assert len(_sent(server, "/api/agent/inventory")) == 2
    assert len(_sent(server, "/api/agent/apps")) == 1

    agent = make_agent()                                      # restart: hash tersimpan, aplikasi tidak dikirim
    agent.tick()
    assert len(_sent(server, "/api/agent/apps")) == 1


def test_refresh_inventory_command_resends_apps(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    agent.tick()
    server.commands = [{"type": "refresh_inventory"}]
    clock.advance(30)
    agent.tick()
    server.commands = []
    clock.advance(5)
    agent.tick()
    assert len(_sent(server, "/api/agent/inventory")) == 2
    assert len(_sent(server, "/api/agent/apps")) == 2       # dipaksa walau hash sama


def test_pending_pc_sends_inventory_after_approval(env):
    data_dir, clock, server, make_agent = env
    agent = make_agent()
    run_for(agent, clock, 120)
    assert not _sent(server, "/api/agent/inventory")
    server.status = "online"
    run_for(agent, clock, 35)
    assert len(_sent(server, "/api/agent/inventory")) == 1 and len(_sent(server, "/api/agent/apps")) == 1


def test_inventory_transient_failure_retries_in_10_minutes(env):
    data_dir, clock, server, make_agent = env
    server.status = "online"
    agent = make_agent()
    server.fail["/api/agent/inventory"] = 503
    agent.tick()
    assert 590 <= agent.due["inventory"] - clock.mono() <= 610
    assert StateStore(data_dir / "state.dat").load().apps_hash is None
    del server.fail["/api/agent/inventory"]
    run_for(agent, clock, 610, step=10)
    assert len(_sent(server, "/api/agent/apps")) == 1


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="kolektor Windows")
def test_windows_collectors_real():
    found = apps.collect()
    assert found and all(a["name"] for a in found)
    inv = inventory.InventoryCollector().collect(None)
    assert inv["os_name"].startswith("Windows") and inv["disks"] and inv["adapters"]
    assert any(d["mount"] == "C:" for d in inv["disks"])
