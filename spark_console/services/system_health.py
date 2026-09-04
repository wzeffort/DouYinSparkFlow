from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


RESOURCE_KEYS = {
    "cpu_percent",
    "memory_percent",
    "disk_percent",
    "disk_free_bytes",
}
TRAFFIC_KEYS = {
    "rx_rate_bps",
    "tx_rate_bps",
    "today_rx_bytes",
    "today_tx_bytes",
    "month_rx_bytes",
    "month_tx_bytes",
}
SERVICE_KEYS = {
    "spark-web",
    "spark-worker",
    "spark-auth",
    "spark-notifier",
    "bps-web",
    "bps-db",
}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _number(value, default=0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _disk_severity(percent: float, free_bytes: float) -> str:
    if percent >= 92 or free_bytes < 2 * 1024**3:
        return "critical"
    if percent >= 85:
        return "warning"
    if percent >= 75:
        return "notice"
    return "healthy"


def load_health_snapshot(
    path: Path, now: datetime | None = None
) -> dict[str, object]:
    current = _utc(now or datetime.now(timezone.utc))
    unavailable = {
        "available": False,
        "stale": True,
        "overall": "critical",
        "message": "采集数据暂不可用",
        "collected_at": None,
        "resources": {},
        "traffic": {},
        "services": {},
        "history": [],
    }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            return unavailable
        collected_at = datetime.fromisoformat(str(raw["collected_at"]).replace("Z", "+00:00"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return unavailable

    resources_raw = raw.get("resources") if isinstance(raw.get("resources"), dict) else {}
    resources = {key: _number(resources_raw.get(key)) for key in RESOURCE_KEYS}
    resources["disk_severity"] = _disk_severity(
        resources["disk_percent"], resources["disk_free_bytes"]
    )
    traffic_raw = raw.get("traffic") if isinstance(raw.get("traffic"), dict) else {}
    traffic = {key: _number(traffic_raw.get(key)) for key in TRAFFIC_KEYS}
    services_raw = raw.get("services") if isinstance(raw.get("services"), dict) else {}
    services = {
        key: str(services_raw.get(key, "unknown"))[:24]
        for key in SERVICE_KEYS
        if key in services_raw
    }
    history = []
    for item in raw.get("history", [])[-2016:]:
        if not isinstance(item, dict) or "at" not in item:
            continue
        history.append(
            {
                "at": str(item["at"])[:40],
                "cpu": _number(item.get("cpu")),
                "memory": _number(item.get("memory")),
                "disk": _number(item.get("disk")),
                "rx_rate": _number(item.get("rx_rate")),
                "tx_rate": _number(item.get("tx_rate")),
            }
        )

    stale = _utc(collected_at) < current - timedelta(minutes=3)
    service_problem = any(value not in {"running", "healthy"} for value in services.values())
    severity_rank = {"healthy": 0, "notice": 1, "warning": 2, "critical": 3}
    overall = resources["disk_severity"]
    if service_problem or stale:
        overall = "critical"
    elif severity_rank.get(overall, 0) < 1 and (
        resources["cpu_percent"] >= 85 or resources["memory_percent"] >= 85
    ):
        overall = "warning"
    return {
        "available": True,
        "stale": stale,
        "overall": overall,
        "message": "采集已超过 3 分钟未更新" if stale else "采集正常",
        "collected_at": _utc(collected_at),
        "resources": resources,
        "traffic": traffic,
        "services": services,
        "history": history,
    }
