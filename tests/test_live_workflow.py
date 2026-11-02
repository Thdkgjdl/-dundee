import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("app", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class LiveWorkflowTests(unittest.TestCase):
    today = "2026-10-15"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "local.key").write_bytes(b"L" * 32)
        (self.data / "settings.json").write_text('{"time":"09:00"}', encoding="utf-8")
        self.data_patch = patch.object(app, "DATA", self.data)
        self.data_patch.start()
        self.clock_patch = patch.object(
            app, "now", return_value=datetime(2026, 10, 15, 9, 0, tzinfo=app.TZ)
        )
        self.clock_patch.start()

    def tearDown(self):
        self.clock_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def page(self, records=None, *, business_date=today, ready=True, status="connected"):
        records = records or [
            {"id": "20230001", "name": "张三", "status": "未打卡"},
            {"id": "20230002", "name": "李四", "status": "未打卡"},
        ]
        revision = app.school_revision(records, business_date) if ready else ""
        return {
            "status": status,
            "title": "班级打卡看板",
            "business_date": business_date,
            "business_route": "/ywViews/qxjlfx/dqr/board",
            "source": "official-h5-safety-board",
            "roster_ready": ready,
            "roster": records if ready else [],
            "revision": revision,
            "text": "脱敏页面样本",
        }

    def snapshot(self, records=None, *, business_date=today, at="2026-10-15T08:30:00+08:00"):
        records = records or self.page()["roster"]
        (self.data / "school-snapshot.json").write_text(json.dumps({
            "date": business_date,
            "at": at,
            "digest": app.school_revision(records, business_date),
            "count": len(records),
            "source": "official-h5-safety-board",
        }), encoding="utf-8")

    def test_school_verification_historical_date_is_view_only(self):
        result = app.school_verification(self.page(business_date="2026-10-14")["roster"], "2026-10-14")
        self.assertEqual(result, {"verification": "历史名单，仅供查看", "verified_at": ""})

    def test_school_verification_without_snapshot_is_unverified(self):
        result = app.school_verification(self.page()["roster"], self.today)
        self.assertIn("尚未整体核验", result["verification"])
        self.assertEqual(result["verified_at"], "")

    def test_school_verification_matching_snapshot_exposes_verification_time(self):
        self.snapshot()
        result = app.school_verification(self.page()["roster"], self.today)
        self.assertIn("名单已核验", result["verification"])
        self.assertEqual(result["verified_at"], "2026-10-15T08:30:00+08:00")

    def test_school_verification_changed_digest_pauses(self):
        self.snapshot()
        changed = [{"id": "20230001", "name": "张三", "status": "未打卡"}]
        result = app.school_verification(changed, self.today)
        self.assertIn("变化", result["verification"])
        self.assertEqual(result["verified_at"], "")

    def test_school_verification_wrong_source_does_not_verify(self):
        self.snapshot()
        snapshot_path = self.data / "school-snapshot.json"
        stored = json.loads(snapshot_path.read_text(encoding="utf-8"))
        stored["source"] = "official-h5-assist"
        snapshot_path.write_text(json.dumps(stored), encoding="utf-8")
        result = app.school_verification(self.page()["roster"], self.today)
        self.assertNotIn("名单已核验", result["verification"])
        self.assertEqual(result["verified_at"], "")

    def test_verify_school_roster_writes_minimal_snapshot_without_personal_data(self):
        live = self.page()
        with patch.object(app, "open_school_roster", return_value=live) as open_roster:
            result = app.verify_school_roster(live["revision"], True)
        open_roster.assert_called_once_with(self.today)
        self.assertTrue(result["ok"])
        stored = json.loads((self.data / "school-snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(set(stored), {"date", "at", "digest", "count", "source"})
        raw = (self.data / "school-snapshot.json").read_text(encoding="utf-8")
        self.assertNotIn("张三", raw)
        self.assertNotIn("20230001", raw)
        self.assertEqual(stored["digest"], live["revision"])
        self.assertEqual(stored["count"], 2)
        self.assertEqual(stored["source"], "official-h5-safety-board")

    def test_verify_school_roster_rejects_unacknowledged(self):
        with self.assertRaisesRegex(ValueError, "逐人核实"):
            app.verify_school_roster("ignored", False)
        self.assertFalse((self.data / "school-snapshot.json").exists())

    def test_verify_school_roster_rejects_revision_mismatch(self):
        live = self.page()
        with patch.object(app, "open_school_roster", return_value=live):
            with self.assertRaisesRegex(ValueError, "状态已变化"):
                app.verify_school_roster("wrong-revision", True)
        self.assertFalse((self.data / "school-snapshot.json").exists())

    def test_verify_school_roster_rejects_non_current_date(self):
        live = self.page(business_date="2026-10-14")
        with patch.object(app, "open_school_roster", return_value=live):
            with self.assertRaisesRegex(ValueError, "今天的完整官方名单"):
                app.verify_school_roster(live["revision"], True)
        self.assertFalse((self.data / "school-snapshot.json").exists())

    def test_verify_school_roster_rejects_incomplete_page(self):
        live = self.page(ready=False)
        with patch.object(app, "open_school_roster", return_value=live):
            with self.assertRaisesRegex(ValueError, "完整官方名单"):
                app.verify_school_roster("", True)
        self.assertFalse((self.data / "school-snapshot.json").exists())

    def test_verify_school_roster_rejects_non_board_source(self):
        live = dict(self.page(), source="official-h5-assist", business_route="")
        with patch.object(app, "open_school_roster", return_value=live):
            with self.assertRaises(ValueError):
                app.verify_school_roster(live["revision"], True)
        self.assertFalse((self.data / "school-snapshot.json").exists())

    def test_scheduled_readonly_check_matches_live_snapshot(self):
        live = self.page()
        self.snapshot()
        with patch.object(app, "open_school_roster", return_value=live) as open_roster:
            result = app.scheduled_readonly_check()
        self.assertEqual(result, "match")
        open_roster.assert_called_once_with(self.today)
        audit = (self.data / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("readonly_match", audit)

    def test_scheduled_readonly_check_pauses_on_changed_or_invalid_page(self):
        live = self.page()
        self.snapshot()
        changed = dict(live, revision="changed")
        with patch.object(app, "open_school_roster", return_value=changed):
            self.assertEqual(app.scheduled_readonly_check(), "paused")

        # A fresh date marker models the next scheduled day and a page that
        # cannot provide a complete current roster must also stop safely.
        (self.data / "scheduled-2026-10-15.done").unlink()
        invalid = self.page(ready=False)
        with patch.object(app, "open_school_roster", return_value=invalid):
            self.assertEqual(app.scheduled_readonly_check(), "paused")

    def test_scheduled_readonly_check_records_error_and_prevents_repeat(self):
        with patch.object(app, "open_school_roster", side_effect=RuntimeError("offline")):
            self.assertEqual(app.scheduled_readonly_check(), "error")
        # The marker is written before the read so an error cannot be retried
        # automatically in the same day.
        self.assertEqual(app.scheduled_readonly_check(), "already-run")
        audit = (self.data / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("readonly_error", audit)

    def test_scheduled_readonly_check_does_not_run_after_configured_minute(self):
        with patch.object(app, "now", return_value=datetime(2026, 10, 15, 9, 1, tzinfo=app.TZ)), \
             patch.object(app, "open_school_roster") as open_roster:
            self.assertEqual(app.scheduled_readonly_check(), "missed")
        open_roster.assert_not_called()
        self.assertFalse((self.data / "scheduled-2026-10-15.done").exists())


if __name__ == "__main__":
    unittest.main()
