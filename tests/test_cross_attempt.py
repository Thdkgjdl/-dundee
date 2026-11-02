"""Offline cross-attempt guards for the supervised trial and batch workflows."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from official_adapter import BatchResult


SPEC = importlib.util.spec_from_file_location("app_cross_attempt", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class CrossAttemptTests(unittest.TestCase):
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
        app.BATCHES.clear()
        app.BATCH_RUNNING.clear()

    def tearDown(self):
        app.TRIALS.clear()
        app.BATCHES.clear()
        app.BATCH_RUNNING.clear()
        self.clock_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def records(self):
        return [
            {"id": "20230001", "name": "测试甲", "status": "未打卡"},
            {"id": "20230002", "name": "测试乙", "status": "未打卡"},
        ]

    def page(self, records=None, **changes):
        records = self.records() if records is None else records
        value = {
            "status": "connected", "title": "班级打卡看板", "business_date": self.today,
            "business_route": "/ywViews/qxjlfx/dqr/board", "source": app.SAFETY_SOURCE,
            "roster_ready": True, "roster": records,
            "revision": app.school_revision(records, self.today), "text": "脱敏页面样本",
        }
        value.update(changes)
        return value

    def snapshot(self):
        (self.data / "school-snapshot.json").write_text(json.dumps({
            "date": self.today, "at": "2026-10-15T08:30:00+08:00",
            "digest": self.page()["revision"], "count": len(self.records()),
            "source": app.SAFETY_SOURCE,
        }), encoding="utf-8")

    def prepare_trial(self):
        self.snapshot()
        page = self.page()
        with patch.object(app, "open_school_roster", return_value=page):
            return app.prepare_school_trial({
                "revision": page["revision"], "record_id": "20230001",
                "method": "电话", "explanation": "已电话核实安全",
            })

    def prepare_batch(self):
        self.snapshot()
        page = self.page()
        with patch.object(app, "open_school_roster", return_value=page):
            return app.prepare_school_batch({
                "revision": page["revision"], "records": [
                    {"id": "20230001", "method": "电话", "explanation": "已电话核实安全"},
                    {"id": "20230002", "method": "电话", "explanation": "已电话核实安全"},
                ],
            })

    def fake_playwright(self, *, pages=True):
        page = types.SimpleNamespace(
            url="https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board"
        )
        browser = types.SimpleNamespace(
            contexts=[types.SimpleNamespace(pages=[page] if pages else [])]
        )
        runtime = Mock(name="runtime")
        runtime.chromium.connect_over_cdp.return_value = browser
        package = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(name="sync_playwright")
        sync_api.sync_playwright.return_value.start.return_value = runtime
        package.sync_api = sync_api
        return patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": sync_api}), sync_api.sync_playwright, runtime

    def test_batch_marker_blocks_trial_before_browser(self):
        trial = self.prepare_trial()
        (self.data / f"batch-attempt-{self.today}.done").write_text("09:00:00", encoding="ascii")
        modules, sync, runtime = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), modules:
            with self.assertRaisesRegex(ValueError, "批量|批次"):
                app.submit_school_trial({"token": trial["token"], "supervised_approved": True})
        sync.assert_not_called()
        runtime.stop.assert_not_called()

    def test_trial_marker_without_result_blocks_batch(self):
        batch = self.prepare_batch()
        (self.data / f"trial-attempt-{self.today}.done").write_text("09:00:00", encoding="ascii")
        modules, sync, runtime = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), modules:
            with self.assertRaisesRegex(ValueError, "试验|结果|核对"):
                app.submit_school_batch({"token": batch["token"], "supervised_approved": True})
        sync.assert_not_called()
        runtime.stop.assert_not_called()

    def test_trial_marker_with_non_success_result_blocks_batch(self):
        for status in ("unknown", "partial", "stopped", "error"):
            with self.subTest(status=status):
                batch = self.prepare_batch()
                (self.data / f"trial-attempt-{self.today}.done").write_text("09:00:00", encoding="ascii")
                (self.data / "trial-result.json").write_text(json.dumps({
                    "at": "2026-10-15T09:00:00+08:00", "status": status,
                    "detail": "脱敏结果",
                }), encoding="utf-8")
                modules, sync, runtime = self.fake_playwright()
                with patch.object(app, "open_school_roster", return_value=self.page()), modules:
                    with self.assertRaisesRegex(ValueError, "试验|结果|核对"):
                        app.submit_school_batch({"token": batch["token"], "supervised_approved": True})
                sync.assert_not_called()
                runtime.stop.assert_not_called()
                app.BATCHES.clear()

    def test_same_day_successful_trial_allows_batch_gate(self):
        batch = self.prepare_batch()
        (self.data / f"trial-attempt-{self.today}.done").write_text("09:00:00", encoding="ascii")
        (self.data / "trial-result.json").write_text(json.dumps({
            "at": "2026-10-15T09:00:00+08:00", "status": "success",
            "submitted": True, "detail": "脱敏成功",
        }), encoding="utf-8")
        modules, sync, runtime = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             patch("official_adapter.execute_batch", return_value=BatchResult("success", "脱敏批次成功", 2, 2)), modules:
            result = app.submit_school_batch({"token": batch["token"], "supervised_approved": True})
        self.assertEqual(result["status"], "success")
        sync.assert_called_once_with()
        runtime.stop.assert_called_once_with()

    def test_trial_exception_persists_unknown_result_without_exception_text(self):
        trial = self.prepare_trial()
        modules, sync, runtime = self.fake_playwright(pages=False)
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), modules:
            with self.assertRaisesRegex(RuntimeError, "试验已停止"):
                app.submit_school_trial({"token": trial["token"], "supervised_approved": True})
        result = json.loads((self.data / "trial-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("测试甲", json.dumps(result, ensure_ascii=False))
        self.assertNotIn(trial["token"], json.dumps(result, ensure_ascii=False))
        sync.assert_called_once_with()
        runtime.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
