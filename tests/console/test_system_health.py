import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spark_console.services.system_health import load_health_snapshot
from ops.spark_health_collector import merge_history, write_json_atomic


class SystemHealthSnapshotTests(unittest.TestCase):
    def test_missing_snapshot_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            view = load_health_snapshot(
                Path(temp) / "missing.json",
                datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
            )

        self.assertFalse(view["available"])
        self.assertEqual("采集数据暂不可用", view["message"])

    def test_valid_snapshot_derives_disk_alert_without_exposing_unknown_fields(self):
        now = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "health.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "collected_at": (now - timedelta(seconds=30)).isoformat(),
                        "resources": {
                            "cpu_percent": 12.5,
                            "memory_percent": 42.0,
                            "disk_percent": 86.0,
                            "disk_free_bytes": 5_000_000_000,
                        },
                        "traffic": {
                            "rx_rate_bps": 2048,
                            "tx_rate_bps": 1024,
                            "today_rx_bytes": 1000,
                            "today_tx_bytes": 2000,
                            "month_rx_bytes": 3000,
                            "month_tx_bytes": 4000,
                        },
                        "services": {"spark-web": "running"},
                        "history": [],
                        "secret": "must-not-leak",
                    }
                ),
                encoding="utf-8",
            )

            view = load_health_snapshot(path, now)

        self.assertTrue(view["available"])
        self.assertFalse(view["stale"])
        self.assertEqual("warning", view["resources"]["disk_severity"])
        self.assertNotIn("secret", view)

    def test_snapshot_older_than_three_minutes_is_stale(self):
        now = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "health.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "collected_at": (now - timedelta(minutes=4)).isoformat(),
                        "resources": {},
                        "traffic": {},
                        "services": {},
                        "history": [],
                    }
                ),
                encoding="utf-8",
            )

            view = load_health_snapshot(path, now)

        self.assertTrue(view["available"])
        self.assertTrue(view["stale"])
        self.assertEqual("采集已超过 3 分钟未更新", view["message"])

    def test_collector_history_is_five_minute_sampled_and_bounded_to_seven_days(self):
        now = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        old = {
            "at": (now - timedelta(days=8)).isoformat(),
            "cpu": 1.0,
            "memory": 2.0,
            "disk": 3.0,
            "rx_rate": 4,
            "tx_rate": 5,
        }
        recent = dict(old, at=(now - timedelta(minutes=4)).isoformat())

        merged = merge_history([old, recent], now, {"cpu": 9.0})

        self.assertEqual(1, len(merged))
        self.assertEqual(recent["at"], merged[0]["at"])

        appended = merge_history(merged, now + timedelta(minutes=1), {"cpu": 9.0})
        self.assertEqual(2, len(appended))
        self.assertEqual(9.0, appended[-1]["cpu"])

    def test_collector_replaces_snapshot_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "host-health.json"
            path.write_text('{"old": true}', encoding="utf-8")

            write_json_atomic(path, {"schema_version": 1, "safe": True})

            self.assertEqual(
                {"schema_version": 1, "safe": True},
                json.loads(path.read_text(encoding="utf-8")),
            )
            self.assertFalse(path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
