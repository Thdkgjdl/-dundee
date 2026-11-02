"""Offline coverage for the supervised batch API; no school browser is used."""
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


SPEC = importlib.util.spec_from_file_location("app_batch_workflow", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class BatchWorkflowTests(unittest.TestCase):
    today = "2026-10-15"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "local.key").write_bytes(b"L" * 32)
        (self.data / "settings.json").write_text('{"time":"09:00"}', encoding="utf-8")
        self.data_patch = patch.object(app, "DATA", self.data)
        self.data_patch.start()
        self.clock_patch = patch.object(app, "now", return_value=datetime(2026, 10, 15, 9, 0, tzinfo=app.TZ))
        self.clock_patch.start()
        app.BATCHES.clear()
        app.BATCH_RUNNING.clear()

    def tearDown(self):
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

    def plans(self):
        return [
            {"id": "20230001", "method": "电话", "explanation": "已电话核实安全"},
            {"id": "20230002", "method": "当面", "explanation": "已当面核实安全"},
        ]

    def page(self, records=None, **changes):
        records = self.records() if records is None else records
        page = {
            "status": "connected", "title": "班级打卡看板", "business_date": self.today,
            "business_route": "/ywViews/qxjlfx/dqr/board", "source": app.SAFETY_SOURCE,
            "roster_ready": True, "roster": records,
            "revision": app.school_revision(records, self.today), "text": "脱敏页面样本",
        }
        page.update(changes)
        return page

    def snapshot(self, **changes):
        snapshot = {
            "date": self.today, "at": "2026-10-15T08:30:00+08:00",
            "digest": self.page()["revision"], "count": len(self.records()), "source": app.SAFETY_SOURCE,
        }
        snapshot.update(changes)
        (self.data / "school-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def prepare(self, plans=None):
        self.snapshot()
        with patch.object(app, "open_school_roster", return_value=self.page()):
            return app.prepare_school_batch({
                "revision": self.page()["revision"], "records": self.plans() if plans is None else plans,
            })

    def fake_playwright(self, *, pages=None):
        """Never import or start the real Playwright implementation in these tests."""
        page = types.SimpleNamespace(url="https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board")
        browser = types.SimpleNamespace(contexts=[types.SimpleNamespace(pages=[page] if pages is None else pages)])
        runtime = Mock(name="fake_runtime")
        runtime.chromium.connect_over_cdp.return_value = browser
        package = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(name="fake_sync_playwright")
        sync_api.sync_playwright.return_value.start.return_value = runtime
        package.sync_api = sync_api
        return patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": sync_api}), sync_api.sync_playwright, runtime, page

    def submit(self, token):
        return app.submit_school_batch({"token": token, "supervised_approved": True})

    def assert_no_personal_data_on_disk(self, token):
        forbidden = [token, "测试甲", "测试乙", "20230001", "20230002", "已电话核实安全", "已当面核实安全"]
        for path in self.data.iterdir():
            if path.is_file() and path.name != "local.key":
                raw = path.read_text(encoding="utf-8")
                for value in forbidden:
                    self.assertNotIn(value, raw, f"{path.name} leaked {value}")

    def test_prepare_orders_complete_preview_without_persisting_personal_data(self):
        result = self.prepare(list(reversed(self.plans())))
        self.assertEqual(result["date"], self.today)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["records"], [
            {**plan, "name": row["name"]} for plan, row in zip(self.plans(), self.records())
        ])
        self.assertIn("不会自动重试", result["notice"])
        self.assertIn(result["token"], app.BATCHES)
        self.assertEqual(set(p.name for p in self.data.iterdir()), {"local.key", "settings.json", "school-snapshot.json"})
        self.assert_no_personal_data_on_disk(result["token"])

    def test_prepare_invalid_fields_are_rejected_before_browser_read(self):
        for supplied in (None, [], {}, [None], [{"id": ""}],
                         [dict(self.plans()[0], method="猜测")],
                         [dict(self.plans()[0], explanation=" ")],
                         [dict(self.plans()[0], explanation="x" * 101)],
                         [dict(self.plans()[0], explanation=123)],
                         [self.plans()[0], self.plans()[0]]):
            with self.subTest(records=supplied), patch.object(app, "open_school_roster") as read:
                with self.assertRaises(ValueError):
                    app.prepare_school_batch({"revision": "unused", "records": supplied})
                read.assert_not_called()
                self.assertEqual(app.BATCHES, {})

    def test_prepare_requires_exact_current_roster(self):
        self.snapshot()
        invalid = [self.plans()[:1], self.plans() + [dict(self.plans()[0], id="20230003")],
                   [self.plans()[0], dict(self.plans()[1], id="20230003")]]
        for plans in invalid:
            with self.subTest(plans=plans), patch.object(app, "open_school_roster", return_value=self.page()):
                with self.assertRaisesRegex(ValueError, "完整待确认名单"):
                    app.prepare_school_batch({"revision": self.page()["revision"], "records": plans})
        with patch.object(app, "open_school_roster", return_value=self.page()):
            with self.assertRaisesRegex(ValueError, "变化"):
                app.prepare_school_batch({"revision": "stale", "records": self.plans()})
        self.assertEqual(app.BATCHES, {})

    def test_prepare_rejects_invalid_page_and_unverified_snapshot(self):
        for changes in ({"status": "login_required"}, {"roster_ready": False},
                        {"source": "official-h5-assist"}, {"business_date": "2026-10-14"}):
            with self.subTest(page=changes), patch.object(app, "open_school_roster", return_value=self.page(**changes)):
                with self.assertRaises(ValueError):
                    app.prepare_school_batch({"revision": self.page()["revision"], "records": self.plans()})
        for changes in (None, {"count": 1}, {"source": "official-h5-assist"},
                        {"date": "2026-10-14"}, {"digest": "changed"}):
            if changes is not None:
                self.snapshot(**changes)
            with self.subTest(snapshot=changes), patch.object(app, "open_school_roster", return_value=self.page()):
                with self.assertRaisesRegex(ValueError, "整体核实"):
                    app.prepare_school_batch({"revision": self.page()["revision"], "records": self.plans()})
        self.assertEqual(app.BATCHES, {})

    def test_new_preview_invalidates_previous_token(self):
        first = self.prepare()
        second = self.prepare()
        self.assertNotIn(first["token"], app.BATCHES)
        self.assertEqual(set(app.BATCHES), {second["token"]})

    def test_submit_requires_explicit_supervision_without_consuming_preview(self):
        prepared = self.prepare()
        for consent in (None, False, 1, "true"):
            modules_patch, sync, _, _ = self.fake_playwright()
            with self.subTest(consent=consent), modules_patch:
                with self.assertRaisesRegex(ValueError, "监督"):
                    app.submit_school_batch({"token": prepared["token"], "supervised_approved": consent})
                sync.assert_not_called()
                self.assertIn(prepared["token"], app.BATCHES)

    def test_submit_rejects_expired_token_or_changed_date_before_browser(self):
        for expired in (True, False):
            prepared = self.prepare()
            modules_patch, sync, _, _ = self.fake_playwright()
            clock = (patch.object(app.monotonic_time, "monotonic", return_value=app.BATCHES[prepared["token"]]["expires"] + 1)
                     if expired else patch.object(app, "now", return_value=datetime(2026, 10, 16, 9, 0, tzinfo=app.TZ)))
            with self.subTest(expired=expired), clock, modules_patch, patch.object(app, "open_school_roster") as read:
                with self.assertRaisesRegex(ValueError, "失效"):
                    self.submit(prepared["token"])
                read.assert_not_called()
                sync.assert_not_called()
                self.assertNotIn(prepared["token"], app.BATCHES)
            self.assertFalse((self.data / f"batch-attempt-{self.today}.done").exists())

    def test_submit_page_or_snapshot_changes_pause_before_attempt(self):
        for changes in ({"status": "login_required"}, {"roster_ready": False}, {"source": "official-h5-assist"},
                        {"business_date": "2026-10-14"}, {"revision": "changed"}):
            prepared = self.prepare()
            modules_patch, sync, _, _ = self.fake_playwright()
            with self.subTest(page=changes), patch.object(app, "open_school_roster", return_value=self.page(**changes)), modules_patch:
                with self.assertRaisesRegex(ValueError, "变化"):
                    self.submit(prepared["token"])
                sync.assert_not_called()
                self.assertFalse((self.data / f"batch-attempt-{self.today}.done").exists())
        for changes in ({"count": 1}, {"source": "official-h5-assist"}, {"date": "2026-10-14"}, {"digest": "changed"}):
            prepared = self.prepare()
            self.snapshot(**changes)
            modules_patch, sync, _, _ = self.fake_playwright()
            with self.subTest(snapshot=changes), patch.object(app, "open_school_roster", return_value=self.page()), modules_patch:
                with self.assertRaisesRegex(ValueError, "快照"):
                    self.submit(prepared["token"])
                sync.assert_not_called()
                self.assertFalse((self.data / f"batch-attempt-{self.today}.done").exists())

    def test_preexisting_persistent_marker_prevents_browser_connection(self):
        prepared = self.prepare()
        marker = self.data / f"batch-attempt-{self.today}.done"
        marker.write_text("previous process attempted this batch", encoding="ascii")
        modules_patch, sync, _, _ = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), modules_patch:
            with self.assertRaisesRegex(ValueError, "已尝试"):
                self.submit(prepared["token"])
        sync.assert_not_called()
        self.assertEqual(marker.read_text(encoding="ascii"), "previous process attempted this batch")

    def test_success_preserves_order_tracks_verified_progress_and_stops_runtime(self):
        prepared = self.prepare()
        modules_patch, sync, runtime, fake_page = self.fake_playwright()
        observed = []
        fake_manager = sync.return_value

        def start_fake_playwright():
            self.assertTrue((self.data / f"batch-attempt-{self.today}.done").exists())
            self.assertTrue(app.BATCH_RUNNING.is_set())
            return fake_manager

        sync.side_effect = start_fake_playwright

        def execute(config, plans, roster, business_date, *, on_progress):
            self.assertTrue((self.data / f"batch-attempt-{self.today}.done").exists())
            self.assertTrue(app.BATCH_RUNNING.is_set())
            self.assertIs(config.page, fake_page)
            self.assertIs(config.supervised_approved, True)
            self.assertEqual(config.board_filter, "pending")
            self.assertEqual(plans, self.plans())
            self.assertEqual(roster, self.records())
            self.assertEqual(business_date, self.today)
            for completed in (1, 2):
                on_progress(completed)
                observed.append(json.loads((self.data / "batch-result.json").read_text(encoding="utf-8"))["submitted_count"])
            return BatchResult("success", "批次各条记录均已通过官方看板结果校验", 2, 2)

        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             patch("official_adapter.execute_batch", side_effect=execute) as adapter, modules_patch:
            result = self.submit(prepared["token"])
        self.assertEqual(result["status"], "success")
        self.assertEqual((result["submitted_count"], result["total_count"]), (2, 2))
        self.assertEqual(observed, [1, 2])
        adapter.assert_called_once()
        sync.assert_called_once_with()
        runtime.stop.assert_called_once_with()
        self.assertFalse(app.BATCH_RUNNING.is_set())
        self.assertNotIn(prepared["token"], app.BATCHES)
        self.assert_no_personal_data_on_disk(prepared["token"])

    def test_partial_result_is_reported_without_retry_and_audit_stays_minimal(self):
        prepared = self.prepare()
        modules_patch, _, runtime, _ = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             patch("official_adapter.execute_batch", return_value=BatchResult("partial", "有记录结果不明，已停止", 1, 2)) as adapter, modules_patch:
            result = self.submit(prepared["token"])
        self.assertEqual((result["status"], result["submitted_count"], result["total_count"]), ("partial", 1, 2))
        adapter.assert_called_once()
        runtime.stop.assert_called_once_with()
        self.assertFalse(app.BATCH_RUNNING.is_set())
        self.assertTrue((self.data / f"batch-attempt-{self.today}.done").exists())
        self.assert_no_personal_data_on_disk(prepared["token"])

    def test_exception_after_progress_is_unknown_without_leaking_exception_text(self):
        prepared = self.prepare()
        modules_patch, _, runtime, _ = self.fake_playwright()

        def fail(config, plans, roster, business_date, *, on_progress):
            on_progress(1)
            raise TimeoutError(f"测试乙 20230002 已当面核实安全 {prepared['token']}")

        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             patch("official_adapter.execute_batch", side_effect=fail) as adapter, modules_patch:
            with self.assertRaisesRegex(RuntimeError, "批次已停止"):
                self.submit(prepared["token"])
        result = json.loads((self.data / "batch-result.json").read_text(encoding="utf-8"))
        self.assertEqual((result["status"], result["submitted_count"], result["total_count"]), ("unknown", 1, 2))
        adapter.assert_called_once()
        runtime.stop.assert_called_once_with()
        self.assertFalse(app.BATCH_RUNNING.is_set())
        self.assert_no_personal_data_on_disk(prepared["token"])

    def all_roster(self, statuses):
        return {"records": [{"id": row["id"], "name": row["name"], "status": status}
                            for row, status in zip(self.records(), statuses)],
                "counts": {}}

    def test_final_roster_verification_confirms_complete_batch(self):
        prepared = self.prepare()
        modules_patch, _, runtime, _ = self.fake_playwright()
        roster = self.all_roster(["已代确认", "已代确认"])
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             patch("official_adapter.execute_batch",
                   return_value=BatchResult("success", "批次各条记录均已通过官方看板结果校验", 2, 2)), \
             patch.object(app, "read_all_roster", return_value=roster), modules_patch:
            result = self.submit(prepared["token"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["confirmed_count"], 2)
        self.assertIn("最终名单核验", result["detail"])
        self.assert_no_personal_data_on_disk(prepared["token"])

    def test_final_roster_mismatch_downgrades_success_to_unknown(self):
        prepared = self.prepare()
        modules_patch, _, runtime, _ = self.fake_playwright()
        roster = self.all_roster(["已代确认", "未打卡"])
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             patch("official_adapter.execute_batch",
                   return_value=BatchResult("success", "批次各条记录均已通过官方看板结果校验", 2, 2)), \
             patch.object(app, "read_all_roster", return_value=roster), modules_patch:
            result = self.submit(prepared["token"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["confirmed_count"], 1)
        self.assertIn("最终名单核验不一致", result["detail"])
        self.assert_no_personal_data_on_disk(prepared["token"])

    def test_final_roster_read_failure_keeps_verified_result(self):
        prepared = self.prepare()
        modules_patch, _, runtime, _ = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), \
             patch("official_adapter.execute_batch",
                   return_value=BatchResult("success", "批次各条记录均已通过官方看板结果校验", 2, 2)), \
             patch.object(app, "read_all_roster", side_effect=RuntimeError("读取失败")), modules_patch:
            result = self.submit(prepared["token"])
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["confirmed_count"])
        self.assert_no_personal_data_on_disk(prepared["token"])

    def test_failed_attempt_consumes_token_and_new_preview_cannot_bypass_daily_marker(self):
        prepared = self.prepare()
        modules_patch, _, runtime, _ = self.fake_playwright(pages=[])
        with patch.object(app, "open_school_roster", return_value=self.page()), \
             patch.object(app, "school_bridge", return_value=Mock(config=Mock(endpoint="http://127.0.0.1:9252"))), modules_patch:
            with self.assertRaisesRegex(RuntimeError, "批次已停止"):
                self.submit(prepared["token"])
        runtime.stop.assert_called_once_with()
        modules_patch, sync, _, _ = self.fake_playwright()
        with modules_patch, patch.object(app, "open_school_roster") as read:
            with self.assertRaisesRegex(ValueError, "失效"):
                self.submit(prepared["token"])
            read.assert_not_called()
            sync.assert_not_called()
        app.BATCHES.clear()  # Model a service restart; the on-disk attempt marker remains.
        replacement = self.prepare()
        modules_patch, sync, _, _ = self.fake_playwright()
        with patch.object(app, "open_school_roster", return_value=self.page()), modules_patch:
            with self.assertRaisesRegex(ValueError, "已尝试"):
                self.submit(replacement["token"])
            sync.assert_not_called()
        self.assert_no_personal_data_on_disk(replacement["token"])


if __name__ == "__main__":
    unittest.main()
