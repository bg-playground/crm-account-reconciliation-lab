import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from recon_lab.vendor.crashlab.run_log import JsonlAppendLog, UtcClock


def fixed_clock():
    return datetime(2026, 10, 2, 11, 0, 0, tzinfo=timezone.utc)


class RunLogTests(unittest.TestCase):
    def test_append_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "log.jsonl"
            JsonlAppendLog.append(path, {"a": 1})
            JsonlAppendLog.append(path, {"b": [1, 2]})
            self.assertEqual(JsonlAppendLog.read(path), [{"a": 1}, {"b": [1, 2]}])
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1]), {"b": [1, 2]})

    def test_iso_is_utc(self):
        self.assertTrue(UtcClock.iso().endswith(("+00:00", "Z")))
        self.assertIn("2026-10-02T11:00:00", UtcClock.iso(fixed_clock))
        with self.assertRaises(ValueError):
            UtcClock.iso(lambda: datetime(2026, 10, 2))


if __name__ == "__main__":
    unittest.main()
