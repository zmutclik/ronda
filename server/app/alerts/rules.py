"""Teks dan logika perbandingan aturan alert (tanpa akses DB)."""
import operator
from decimal import Decimal

from app.alerts.models import AlertRule
from app.core.templating import duration

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "=": operator.eq}
OP_TEXT = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "="}
UNITS = {"disk_pct": "%", "cpu_pct": "%", "ram_pct": "%", "latency_ms": " ms", "offline_min": " mnt",
         "agent_rollback": "", "uptime_days": " hari"}
# Metrik yang dinilai sepanjang jendela durasi dari tabel metrics; lainnya dari kondisi terakhir
WINDOWED = {"cpu_pct", "ram_pct", "latency_ms"}
METRIC_COLUMN = {"cpu_pct": "cpu_pct", "ram_pct": "ram_pct", "latency_ms": "latency_ms"}


def fmt_threshold(value: Decimal | float) -> str:
    v = float(value)
    return f"{v:g}".replace(".", ",")


def describe(rule: AlertRule) -> str:
    """Kalimat aturan untuk tabel Aturan: "Disk ≥ 90% selama 5 mnt"."""
    thr = f"{OP_TEXT[rule.operator]} {fmt_threshold(rule.threshold)}{UNITS[rule.metric]}"
    if rule.metric == "offline_min":
        return f"Tanpa heartbeat {thr}"
    if rule.metric == "agent_rollback":
        return "Status update = rollback"
    name = {"disk_pct": "Disk", "cpu_pct": "CPU", "ram_pct": "RAM", "latency_ms": "Ping",
            "uptime_days": "Uptime"}[rule.metric]
    text = f"{name} {thr}"
    if rule.duration_sec and rule.metric in WINDOWED | {"disk_pct"}:
        text += f" selama {duration(rule.duration_sec)}"
    return text


def short_rule(rule: AlertRule, mount: str | None = None) -> str:
    """Kolom "Aturan" di tabel alert: "Disk C: ≥ 90%", "CPU > 95% · 15 mnt"."""
    thr = f"{OP_TEXT[rule.operator]} {fmt_threshold(rule.threshold)}{UNITS[rule.metric]}"
    if rule.metric == "disk_pct":
        return f"Disk {mount or ''} {thr}".replace("  ", " ")
    if rule.metric == "offline_min":
        return f"Offline {thr}"
    if rule.metric == "agent_rollback":
        return "Agent rollback"
    name = {"cpu_pct": "CPU", "ram_pct": "RAM", "latency_ms": "Latency", "uptime_days": "Uptime"}[rule.metric]
    text = f"{name} {thr}"
    if rule.duration_sec and rule.metric in WINDOWED:
        text += f" · {duration(rule.duration_sec)}"
    return text


def value_text(metric: str, value: float | Decimal | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if metric == "agent_rollback":
        return "rollback"
    return f"{v:.0f}{UNITS[metric]}"


def problem_text(rule: AlertRule, value: float | None, mount: str | None) -> str:
    """Ringkasan pendek di daftar Komputer/Dashboard: "Disk C: 94%", "Offline 14 mnt"."""
    v = 0.0 if value is None else float(value)
    match rule.metric:
        case "disk_pct":
            return f"Disk {mount or ''} {v:.0f}%".replace("  ", " ")
        case "cpu_pct":
            return f"CPU {v:.0f}%"
        case "ram_pct":
            return f"RAM {v:.0f}%"
        case "latency_ms":
            return f"Latency {v:.0f} ms"
        case "offline_min":
            return f"Offline {duration(v * 60)}"
        case "agent_rollback":
            return "Agent rollback"
        case _:
            return f"Uptime {v:.0f} hari"


def message(rule: AlertRule, value: float | None, mount: str | None) -> str:
    """Pesan alert (Riwayat PC, notifikasi): "Disk C: 94% (ambang 90%)"."""
    thr = f"{fmt_threshold(rule.threshold)}{UNITS[rule.metric]}"
    if rule.metric == "offline_min":
        return f"Offline lebih dari {thr}"
    if rule.metric == "agent_rollback":
        return "Update agent gagal, kembali ke versi sebelumnya"
    text = f"{problem_text(rule, value, mount)} (ambang {thr})"
    if rule.duration_sec and rule.metric in WINDOWED:
        text = f"{problem_text(rule, value, mount)} selama {duration(rule.duration_sec)} (ambang {thr})"
    return text


def check(rule: AlertRule, low: float | None, high: float | None) -> tuple[bool, bool]:
    """(melanggar sepanjang jendela, normal sepanjang jendela) dari nilai minimum & maksimum dalam jendela.
    Untuk nilai tunggal, low = high = nilai. Di antara keduanya: status alert dibiarkan (histeresis)."""
    if low is None or high is None:
        return False, False
    op, thr = OPS[rule.operator], float(rule.threshold)
    if rule.operator in (">", ">="):
        return op(low, thr), not op(high, thr)
    if rule.operator in ("<", "<="):
        return op(high, thr), not op(low, thr)
    return low == high == thr, not (low <= thr <= high)
