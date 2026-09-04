from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


SERVICE_CONTAINERS = {
    "spark-web": "douyin-spark-console-spark-web-1",
    "spark-worker": "douyin-spark-console-spark-worker-1",
    "spark-auth": "douyin-spark-console-spark-auth-1",
    "spark-notifier": "douyin-spark-console-spark-notifier-1",
    "bps-web": "bps-bps-web-1",
    "bps-db": "bps-bps-db-1",
}


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def merge_history(history: list, now: datetime, point: dict) -> list[dict]:
    cutoff = now - timedelta(days=7)
    kept = []
    for item in history[-2016:]:
        try:
            at = datetime.fromisoformat(str(item["at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if at >= cutoff:
            kept.append(item)
    last_at = None
    if kept:
        last_at = datetime.fromisoformat(str(kept[-1]["at"]).replace("Z", "+00:00"))
    if last_at is None or now - last_at >= timedelta(minutes=5):
        kept.append({"at": now.isoformat(), **point})
    return kept[-2016:]


def _read_meminfo() -> tuple[float, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        name, raw = line.split(":", 1)
        values[name] = int(raw.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    percent = ((total - available) / total * 100) if total else 0.0
    return round(percent, 1), available


def _read_cpu() -> tuple[int, int]:
    fields = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def _read_counter(interface: str, name: str) -> int:
    path = Path("/sys/class/net") / interface / "statistics" / name
    return int(path.read_text(encoding="ascii").strip())


def _vnstat_totals(interface: str) -> dict[str, int] | None:
    try:
        result = subprocess.run(
            ["vnstat", "--json"], capture_output=True, text=True, timeout=5, check=True
        )
        data = json.loads(result.stdout)
        entry = next(item for item in data.get("interfaces", []) if item.get("name") == interface)
        traffic = entry.get("traffic", {})
        day = traffic.get("day", [])[-1]
        month = traffic.get("month", [])[-1]
        return {
            "today_rx_bytes": int(day.get("rx", 0)),
            "today_tx_bytes": int(day.get("tx", 0)),
            "month_rx_bytes": int(month.get("rx", 0)),
            "month_tx_bytes": int(month.get("tx", 0)),
        }
    except (FileNotFoundError, StopIteration, subprocess.SubprocessError, ValueError, KeyError, IndexError, json.JSONDecodeError):
        return None


def _service_states() -> dict[str, str]:
    states = {}
    for label, container in SERVICE_CONTAINERS.items():
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", container],
                capture_output=True,
                text=True,
                timeout=4,
                check=True,
            )
            states[label] = result.stdout.strip()[:24] or "unknown"
        except (FileNotFoundError, subprocess.SubprocessError):
            states[label] = "unknown"
    return states


def collect(output: Path, state_path: Path, interface: str) -> dict:
    now = datetime.now(timezone.utc)
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    try:
        old_snapshot = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old_snapshot = {}

    cpu_total, cpu_idle = _read_cpu()
    old_total = int(previous.get("cpu_total", cpu_total))
    old_idle = int(previous.get("cpu_idle", cpu_idle))
    cpu_delta = max(0, cpu_total - old_total)
    cpu_percent = 0.0 if not cpu_delta else (1 - max(0, cpu_idle - old_idle) / cpu_delta) * 100
    memory_percent, memory_available = _read_meminfo()
    disk = shutil.disk_usage("/")
    rx_total = _read_counter(interface, "rx_bytes")
    tx_total = _read_counter(interface, "tx_bytes")
    old_at = float(previous.get("timestamp", now.timestamp()))
    elapsed = max(1.0, now.timestamp() - old_at)
    rx_rate = max(0, rx_total - int(previous.get("rx_total", rx_total))) / elapsed
    tx_rate = max(0, tx_total - int(previous.get("tx_total", tx_total))) / elapsed
    totals = _vnstat_totals(interface) or {
        "today_rx_bytes": rx_total,
        "today_tx_bytes": tx_total,
        "month_rx_bytes": rx_total,
        "month_tx_bytes": tx_total,
    }
    resources = {
        "cpu_percent": round(max(0.0, min(100.0, cpu_percent)), 1),
        "memory_percent": memory_percent,
        "memory_available_bytes": memory_available,
        "disk_percent": round((disk.used / disk.total * 100) if disk.total else 0.0, 1),
        "disk_free_bytes": disk.free,
    }
    traffic = {
        "rx_rate_bps": round(rx_rate, 1),
        "tx_rate_bps": round(tx_rate, 1),
        **totals,
    }
    point = {
        "cpu": resources["cpu_percent"],
        "memory": resources["memory_percent"],
        "disk": resources["disk_percent"],
        "rx_rate": traffic["rx_rate_bps"],
        "tx_rate": traffic["tx_rate_bps"],
    }
    payload = {
        "schema_version": 1,
        "collected_at": now.isoformat(),
        "resources": resources,
        "traffic": traffic,
        "services": _service_states(),
        "history": merge_history(old_snapshot.get("history", []), now, point),
    }
    write_json_atomic(output, payload)
    write_json_atomic(
        state_path,
        {
            "timestamp": now.timestamp(),
            "cpu_total": cpu_total,
            "cpu_idle": cpu_idle,
            "rx_total": rx_total,
            "tx_total": tx_total,
        },
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--interface", default="eth0")
    args = parser.parse_args()
    collect(args.output, args.state, args.interface)


if __name__ == "__main__":
    main()
