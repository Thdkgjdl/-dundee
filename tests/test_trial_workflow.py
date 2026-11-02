import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location("app_trial_workflow", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class TrialWorkflowTests(unittest.TestCase):
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
        app.TRIALS.clear()

    def tearDown(self):
        app.TRIALS.clear()
        self.clock_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def records(self):
        return [
            {"id": "20230001", "name": "测试甲", "status": "未打卡"},
            {"id": "20230002", "name": "测试乙", "status": "未打卡"},
        ]

    def page(self, records=None, *, revision=None, ready=True, source=None):
        records = records or self.records()
        return {
            "status": "connected",
            "title": "班级打卡看板",
            "business_date": self.today,
            "business_route": "/ywViews/qxjlfx/dqr/board",
            "source": source or app.SAFETY_SOURCE,
            "roster_ready": ready,
            "roster": records if ready else [],
            "revision": revision if revision is not None else app.school_revision(records, self.today),
            "text": "脱敏页面样本",
        }

    def snapshot(self, records=None):
        records = records or self.records()
        (self.data / "school-snapshot.json").write_text(json.dumps({
            "date": self.today,
            "at": "2026-10-15T08:30:00+08:00",
            "digest": app.school_revision(records, self.today),
            "count": len(records),
            "source": app.SAFETY_SOURCE,
        }), encoding="utf-8")

    def prepare(self, record_id="20230001", *, page=None):
        records = self.records()
        self.snapshot(records)
        page = page or self.page(records)
        body = {
            "revision": page["revision"],
            "record_id": record_id,
            "method": "电话",
            "explanation": "已电话核实安全",
        }
        with patch.object(app, "open_school_roster", return_value=page):
            return app.prepare_school_trial(body)

    def fake_playwright(self):
        """Install an importable fake so preflight tests cannot touch Playwright."""
        package = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(name="sync_playwright")
        package.sync_api = sync_api
        modules = {"playwright": package, "playwright.sync_api": sync_api}
        return patch.dict(sys.modules, modules), sync_api.sync_playwright

    def test_prepare_returns_reviewable_token_without_persisting_personal_data(self):
        result = self.prepare()

        self.assertEqual(result["record"], self.records()[0])
        self.assertEqual(result["method"], "电话")
        self.assertEqual(result["explanation"], "已电话核实安全")
        self.assertEqual(result["date"], self.today)
        self.assertIn("仅提交这一条", result["notice"])
        self.assertIn(result["token"], app.TRIALS)
        self.assertNotIn("测试甲", (self.data / "school-snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(set(path.name for path in self.data.iterdir()), {"local.key", "settings.json", "school-snapshot.json"})

    def test_prepare_rejects_invalid_method_or_explanation_before_connecting(self):
        page = self.page()
        self.snapshot()
        invalid_bodies = [
            {"method": "猜测", "explanation": "已核实"},
            {"method": "电话", "explanation": ""},
            {"method": "电话", "explanation": "x" * 101},
        ]
        for values in invalid_bodies:
            body = {"revision": page["revision"], "record_id": "20230001", **values}
            with patch.object(app, "open_school_roster") as open_roster:
                with self.assertRaisesRegex(ValueError, "核实方式|情况说明"):
                    app.prepare_school_trial(body)
            open_roster.assert_not_called()

    def test_prepare_requires_current_revision_and_record_id(self):
        page = self.page()
        self.snapshot()
        for body in (
            {"revision": "stale", "record_id": "20230001", "method": "电话", "explanation": "已核实"},
            {"revision": page["revision"], "record_id": "missing", "method": "电话", "explanation": "已核实"},
        ):
            with patch.object(app, "open_school_roster", return_value=page):
                with self.assertRaises(ValueError):
                    app.prepare_school_trial(body)
        self.assertEqual(app.TRIALS, {})

    def test_prepare_requires_verified_safety_board_snapshot(self):
        page = self.page()
        bad_pages = [
            self.page(ready=False),
            self.page(source="official-h5-assist"),
        ]
        for bad_page in bad_pages:
            with patch.object(app, "open_school_roster", return_value=bad_page):
                with self.assertRaises(ValueError):
                    app.prepare_school_trial({
                        "revision": bad_page["revision"],
                        "record_id": "20230001",
                        "method": "电话",
                        "explanation": "已核实",
                    })
        # No verified snapshot exists for the valid page below.
        with patch.object(app, "open_school_roster", return_value=page):
            with self.assertRaisesRegex(ValueError, "整体核实"):
                app.prepare_school_trial({
                    "revision": page["revision"],
                    "record_id": "20230001",
                    "method": "电话",
                    "explanation": "已核实",
                })

    def test_submit_supervision_gate_rejects_before_playwright(self):
        prepared = self.prepare()
        modules_patch, sync_playwright = self.fake_playwright()
        with modules_patch:
            with self.assertRaisesRegex(ValueError, "监督"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": False})
        sync_playwright.assert_not_called()
        self.assertIn(prepared["token"], app.TRIALS)

    def test_submit_rejects_expired_token_and_pops_nonce(self):
        prepared = self.prepare()
        # The prepared response intentionally does not expose the expiry;
        # use the temporary in-memory record to model five-minute expiry.
        expires = app.TRIALS[prepared["token"]]["expires"]
        modules_patch, sync_playwright = self.fake_playwright()
        with patch.object(app.monotonic_time, "monotonic", return_value=expires + 1), modules_patch:
            with self.assertRaisesRegex(ValueError, "失效"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_not_called()
        self.assertNotIn(prepared["token"], app.TRIALS)

    def test_submit_rejects_wrong_business_date_without_connecting(self):
        prepared = self.prepare()
        modules_patch, sync_playwright = self.fake_playwright()
        with patch.object(app, "now", return_value=datetime(2026, 10, 16, 9, 0, tzinfo=app.TZ)), modules_patch:
            with self.assertRaisesRegex(ValueError, "失效"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_not_called()

    def test_submit_rejects_refreshed_revision_before_marker_or_playwright(self):
        prepared = self.prepare()
        changed = self.page(revision="changed")
        modules_patch, sync_playwright = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=changed), modules_patch:
            with self.assertRaisesRegex(ValueError, "变化"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_not_called()
        self.assertFalse((self.data / f"trial-attempt-{self.today}.done").exists())

    def test_submit_rejects_changed_verification_snapshot_before_marker_or_playwright(self):
        prepared = self.prepare()
        snapshot = json.loads((self.data / "school-snapshot.json").read_text(encoding="utf-8"))
        snapshot["digest"] = "changed"
        (self.data / "school-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        modules_patch, sync_playwright = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), modules_patch:
            with self.assertRaisesRegex(ValueError, "快照"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_not_called()
        self.assertFalse((self.data / f"trial-attempt-{self.today}.done").exists())

    def test_submit_marker_is_created_before_any_playwright_connection(self):
        prepared = self.prepare()
        marker = self.data / f"trial-attempt-{self.today}.done"
        marker.write_text("previous attempt", encoding="ascii")
        modules_patch, sync_playwright = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), modules_patch:
            with self.assertRaisesRegex(ValueError, "已经尝试"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_not_called()

    def test_submit_writes_marker_before_fake_browser_attempt(self):
        prepared = self.prepare()
        modules_patch, sync_playwright = self.fake_playwright()
        runtime = Mock()
        runtime.chromium.connect_over_cdp.return_value.contexts = []
        sync_playwright.return_value.start.return_value = runtime
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             modules_patch:
            with self.assertRaisesRegex(RuntimeError, "试验已停止"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_called_once_with()
        runtime.stop.assert_called_once_with()
        self.assertTrue((self.data / f"trial-attempt-{self.today}.done").exists())

    def test_submit_consumes_token_once_and_replay_is_rejected(self):
        prepared = self.prepare()
        # Make the first attempt fail at the refreshed-revision gate.  The
        # nonce is popped before that check, so a second use cannot proceed.
        modules_patch, sync_playwright = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page(revision="changed")), modules_patch:
            with self.assertRaisesRegex(ValueError, "变化"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_not_called()
        self.assertNotIn(prepared["token"], app.TRIALS)
        modules_patch, sync_playwright = self.fake_playwright()
        with modules_patch:
            with self.assertRaisesRegex(ValueError, "失效"):
                app.submit_school_trial({"token": prepared["token"], "supervised_approved": True})
        sync_playwright.assert_not_called()


if __name__ == "__main__":
    unittest.main()
