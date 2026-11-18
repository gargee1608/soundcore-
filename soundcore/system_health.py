"""Host metrics for the cockpit (Linux / Raspberry Pi). No simulated values."""

from __future__ import annotations

import os
import platform
import shutil
import time


def _read_proc_stat() -> tuple[float, float]:
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return 0.0, 0.0
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return 0.0, 0.0
    values = [int(x) for x in parts[1:8]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return float(total), float(idle)


def cpu_usage_percent(sample_s: float = 0.15) -> float | None:
    if platform.system() != "Linux":
        return None
    t0, i0 = _read_proc_stat()
    time.sleep(max(0.05, sample_s))
    t1, i1 = _read_proc_stat()
    dt = t1 - t0
    di = i1 - i0
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - di / dt) * 100.0)), 1)


def memory_usage() -> dict:
    if platform.system() != "Linux":
        total = shutil.disk_usage("/").total if os.name != "nt" else 0
        return {"total_bytes": 0, "available_bytes": 0, "used_ratio": 0.0}
    mem: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    mem[key] = int(rest.strip().split()[0]) * 1024
    except OSError:
        return {"total_bytes": 0, "available_bytes": 0, "used_ratio": 0.0}
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    used_ratio = (total - avail) / total if total else 0.0
    return {
        "total_bytes": total,
        "available_bytes": avail,
        "used_ratio": round(used_ratio, 4),
    }


def cpu_temperature_c() -> float | None:
    base = "/sys/class/thermal"
    if not os.path.isdir(base):
        return None
    best: float | None = None
    for name in sorted(os.listdir(base)):
        if not name.startswith("thermal_zone"):
            continue
        path = os.path.join(base, name, "temp")
        try:
            raw = int(open(path, encoding="utf-8").read().strip())
        except (OSError, ValueError):
            continue
        value = raw / 1000.0
        if best is None or value > best:
            best = value
    return round(best, 1) if best is not None else None


def health_level(*, disk_level: str, capacity_level: str, cpu_pct: float | None, mem_ratio: float, temp_c: float | None) -> str:
    if disk_level == "critical" or capacity_level == "blocked":
        return "critical"
    if temp_c is not None and temp_c >= 80.0:
        return "critical"
    if disk_level == "warning" or capacity_level in {"warn", "warning"}:
        return "warning"
    if cpu_pct is not None and cpu_pct >= 90.0:
        return "warning"
    if mem_ratio >= 0.92:
        return "warning"
    if temp_c is not None and temp_c >= 70.0:
        return "warning"
    return "healthy"


_CACHE: dict = {"at": 0.0, "body": None}


def snapshot(*, disk_level: str = "ok", capacity_level: str = "ok") -> dict:
    now = time.monotonic()
    if _CACHE["body"] is not None and now - _CACHE["at"] < 2.0:
        body = dict(_CACHE["body"])
        body["level"] = health_level(
            disk_level=disk_level,
            capacity_level=capacity_level,
            cpu_pct=body.get("cpu_percent"),
            mem_ratio=body.get("memory", {}).get("used_ratio", 0.0),
            temp_c=body.get("temperature_c"),
        )
        return body
    mem = memory_usage()
    cpu = cpu_usage_percent()
    temp = cpu_temperature_c()
    level = health_level(
        disk_level=disk_level,
        capacity_level=capacity_level,
        cpu_pct=cpu,
        mem_ratio=mem.get("used_ratio", 0.0),
        temp_c=temp,
    )
    body = {
        "level": level,
        "cpu_percent": cpu,
        "memory": mem,
        "temperature_c": temp,
        "platform": platform.platform(),
    }
    _CACHE["at"] = time.monotonic()
    _CACHE["body"] = {k: v for k, v in body.items() if k != "level"}
    return body