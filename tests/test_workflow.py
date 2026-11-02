import importlib.util
import json
import os
import socket
import urllib.error
import urllib.request
import tempfile
import threading
import unittest
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from browser_bridge import BridgeStatus, PageSnapshot

SPEC = importlib.util.spec_from_file_location("app", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "local.key").write_bytes(b"K" * 32)
        (self.data / "roster.json").write_text(json.dumps(app.DEMO), encoding="utf-8")
        (self.data / "settings.json").write_text('{"time":"09:00"}', encoding="utf-8")
        self.data_patch = patch.object(app, "DATA", self.data)
        self.data_patch.start()

    def tearDown(self):
        self.data_patch.stop()
        self.tmp.cleanup()

    def test_roster_sanitization_and_duplicate_rejection(self):
        self.assertEqual(len(app.safe_roster(app.roster())), 2)
        with self.assertRaises(ValueError):
            app.safe_roster([app.DEMO[0], app.DEMO[0]])

    def test_daily_snapshot_matches_then_change_pauses(self):
        items = app.safe_roster(app.roster())
        snap = {"date": app.now().date().isoformat(), "digest": app.digest(items), "count": 2}
        (self.data / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
        self.assertIn("名单一致", app.current_state()["verification"])
        changed = [dict(items[0], status="已确认"), items[1]]
        (self.data / "roster.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        self.assertIn("变化", app.current_state()["verification"])

    def test_snapshot_from_prior_day_expires(self):
        snap = {"date": "2000-01-01", "digest": app.digest(app.safe_roster(app.roster())), "count": 2}
        (self.data / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
        self.assertIn("过期", app.current_state()["verification"])

    def test_malformed_roster_and_snapshot_fail_closed(self):
        roster_path = self.data / "roster.json"
        roster_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            app.current_state()
        roster_path.write_text(json.dumps(app.DEMO), encoding="utf-8")
        (self.data / "snapshot.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            app.current_state()

    def test_schedule_requires_configured_minute_and_runs_once(self):
        fake_now = datetime(2026, 10, 14, 9, 0, tzinfo=app.TZ)
        # The scheduler performs a live school-page read; keep this unit test
        # deterministic and offline by returning an unverified page snapshot.
        with patch.object(app, "now", return_value=fake_now), \
             patch.object(app, "open_school_roster", return_value={
                 "status": "login_required", "roster_ready": False, "roster": []
             }):
            self.assertEqual(app.scheduled_readonly_check(), "paused")
            self.assertEqual(app.scheduled_readonly_check(), "already-run")
        self.assertEqual(len((self.data / "audit.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_missed_time_does_not_check_or_create_run_marker(self):
        fake_now = datetime(2026, 10, 14, 9, 2, tzinfo=app.TZ)
        with patch.object(app, "now", return_value=fake_now):
            self.assertEqual(app.scheduled_readonly_check(), "missed")
        self.assertFalse((self.data / "scheduled-2026-10-14.done").exists())

    def test_submission_endpoint_is_fail_closed(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port)
            conn.request("POST", "/api/submit", "{}", {"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            self.assertIn("未启用", response.read().decode())
        finally:
            server.shutdown()
            server.server_close()

    def test_school_page_snapshot_parses_pending_count_without_demo_fallback(self):
        school_url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board"
        board_text = """2026 级 · 今日晚寝打卡
应打卡
3
已打卡
0
已代确认
0
未打卡
3
距今日打卡截止（23:00）还有 4 小时，点击学生卡片可查看其打卡详情；对确实无法本人打卡但已核实安全的同学，可发起「安全代确认」——该记录不计为本人打卡，也不计缺勤。
全部（3）
未打卡（3）
已打卡（0）
已代确认（0）
测试甲
未打卡
安全代确认
学号 20230001
测试乙
未打卡
安全代确认
学号 20230002
测试丙
未打卡
安全代确认
学号 20230003
Loading"""
        snapshot = PageSnapshot(BridgeStatus.CONNECTED, title="班级打卡看板",
                                    url=school_url,
                                    text=board_text,
                                    backend="playwright", mobile=True,
                                    business_route="/ywViews/qxjlfx/dqr/board",
                                    board_filter="pending")
        with patch.object(app, "school_bridge") as bridge:
            bridge.return_value.read_status.return_value = snapshot
            result = app.inspect_school_h5()
        self.assertEqual(result["pending_count"], 3)
        self.assertTrue(result["list_detected"])
        self.assertTrue(result["roster_ready"])
        self.assertEqual(result["business_route"], "/ywViews/qxjlfx/dqr/board")
        self.assertEqual(result["text"], snapshot.text)
        self.assertNotIn("演示人员", result["text"])
        self.assertEqual(result["status"], "connected")

    def test_assist_page_is_not_a_safety_board_roster_source(self):
        school_url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dkcb/assist?rq=2026-10-15"
        snapshot = PageSnapshot(BridgeStatus.CONNECTED, title="打卡催办协助", url=school_url,
                                text="本班未打卡同学（1）\n甲\n未打卡\n回填\n学号 20230001\n本次已回填 0 / 1",
                                backend="playwright", mobile=True)
        with patch.object(app, "school_bridge") as bridge:
            bridge.return_value.read_status.return_value = snapshot
            result = app.inspect_school_h5()
        self.assertFalse(result["roster_ready"])
        self.assertEqual(result["business_route"], "")

    def test_school_page_snapshot_without_roster_text_stays_read_only(self):
        snapshot = PageSnapshot(BridgeStatus.LOGIN_REQUIRED,
                                    title="统一身份认证", text="请登录")
        with patch.object(app, "school_bridge") as bridge:
            bridge.return_value.read_status.return_value = snapshot
            result = app.inspect_school_h5()
        self.assertIsNone(result["pending_count"])
        self.assertFalse(result["list_detected"])
        self.assertEqual(result["status"], "login_required")

    def test_cdp_port_selection_skips_an_occupied_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            busy_port = occupied.getsockname()[1]
            with patch.object(app, "CDP_PORT", busy_port):
                self.assertGreater(app.find_free_cdp_port(), busy_port)

    def test_fallback_cdp_port_uses_stable_separate_browser_profile(self):
        with patch.object(app, "CDP_PORT", 9252):
            self.assertEqual(app.browser_profile(9252), self.data / "edge-school-direct-mobile")
            self.assertEqual(app.browser_profile(9253), self.data / "edge-school-direct-mobile-9253")

    def test_roster_date_rejects_invalid_and_future_dates_before_navigation(self):
        fixed_now = datetime(2026, 10, 15, 12, 0, tzinfo=app.TZ)
        with patch.object(app, "now", return_value=fixed_now), patch.object(app, "BrowserBridge") as bridge:
            with self.assertRaisesRegex(ValueError, "有效日期"):
                app.open_school_roster("not-a-date")
            with self.assertRaisesRegex(ValueError, "今天"):
                app.open_school_roster("2026-10-16")
        bridge.assert_not_called()

    def test_valid_roster_date_uses_official_h5_route(self):
        fixed_now = datetime(2026, 10, 15, 12, 0, tzinfo=app.TZ)
        with patch.object(app, "now", return_value=fixed_now), \
             patch.object(app, "BrowserBridge") as bridge, \
             patch.object(app, "inspect_school_h5", return_value={
                 "status": "connected", "business_date": "2026-10-15",
                 "business_route": "/ywViews/qxjlfx/dqr/board",
                 "source": "official-h5-safety-board",
             }) as inspect:
            result = app.open_school_roster("2026-10-15")
        self.assertEqual(result["status"], "connected")
        config = bridge.call_args.args[0]
        self.assertEqual(config.school_url,
                         "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board")
        self.assertEqual(config.endpoint, f"http://127.0.0.1:{app.ACTIVE_CDP_PORT}")
        bridge.return_value.open.assert_called_once_with()
        inspect.assert_called_once_with()

    def test_roster_date_rejects_historical_board_until_history_is_verified(self):
        fixed_now = datetime(2026, 10, 15, 12, 0, tzinfo=app.TZ)
        with patch.object(app, "now", return_value=fixed_now), patch.object(app, "BrowserBridge") as bridge:
            with self.assertRaisesRegex(ValueError, "今天"):
                app.open_school_roster("2026-10-14")
        bridge.assert_not_called()

    def test_roster_date_stale_page_fails_closed_after_poll_timeout(self):
        fixed_now = datetime(2026, 10, 15, 12, 0, tzinfo=app.TZ)
        stale_page = {"status": "connected", "business_date": "2026-10-13",
                      "business_route": "/ywViews/qxjlfx/dqr/board", "roster_ready": False,
                      "text": "班级打卡看板"}
        with patch.object(app, "now", return_value=fixed_now), \
             patch.object(app, "BrowserBridge") as bridge, \
             patch.object(app, "inspect_school_h5", return_value=stale_page) as inspect, \
             patch.object(app.monotonic_time, "monotonic", side_effect=[0, 13]):
            result = app.open_school_roster("2026-10-15")
        self.assertEqual(result["status"], "loading_failed")
        self.assertIn("日期未切换", result["detail"])
        bridge.return_value.open.assert_called_once_with()
        inspect.assert_called_once_with()

    def test_school_page_endpoint_rejects_nonlocal_host(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port)
            with patch.object(app, "inspect_school_h5") as inspect:
                conn.request("GET", "/api/school-page", headers={"Host": "evil.example"})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
            inspect.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()

    def test_verification_rejects_roster_changed_after_display(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/state") as response:
                revision = json.loads(response.read())["revision"]
            changed = [dict(app.DEMO[0], status="状态已变化"), app.DEMO[1]]
            (self.data / "roster.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            request = urllib.request.Request(base + "/api/verify",
                                             data=json.dumps({"acknowledged": True, "revision": revision}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 409)
            self.assertFalse((self.data / "snapshot.json").exists())
        finally:
            server.shutdown()
            server.server_close()

class OpenSchoolH5DecisionTests(unittest.TestCase):
    """The open button must launch Edge when nothing listens, and must not
    spawn a second instance when the browser is already running."""

    def bridge_with(self, status, detail=""):
        bridge = Mock()
        bridge.read_status.return_value = PageSnapshot(status, detail=detail)
        return bridge

    def test_dead_endpoint_reaches_the_edge_launcher(self):
        bridge = self.bridge_with(BridgeStatus.ENDPOINT_UNAVAILABLE, "offline")
        previous_port = app.ACTIVE_CDP_PORT
        with tempfile.TemporaryDirectory() as tmp:
            try:
                with patch.object(app, "school_bridge", return_value=bridge), \
                     patch.object(app, "find_free_cdp_port", return_value=9311), \
                     patch.object(app, "browser_profile", return_value=Path(tmp) / "profile"), \
                     patch.object(app.subprocess, "Popen", side_effect=RuntimeError("launch-reached")):
                    with self.assertRaisesRegex(RuntimeError, "launch-reached"):
                        app.open_school_h5()
            finally:
                app.ACTIVE_CDP_PORT = previous_port

    def test_running_browser_is_renavigated_not_relaunched(self):
        for status in (BridgeStatus.LOADING_FAILED, BridgeStatus.NOT_SCHOOL_PAGE,
                       BridgeStatus.MOBILE_REQUIRED):
            with self.subTest(status=status):
                bridge = self.bridge_with(status, "page failed")
                bridge.open.return_value = PageSnapshot(BridgeStatus.CONNECTED)
                with patch.object(app, "school_bridge", return_value=bridge), \
                     patch.object(app.subprocess, "Popen") as popen:
                    result = app.open_school_h5()
                self.assertEqual(result["status"], "connected")
                bridge.open.assert_called_once_with()
                popen.assert_not_called()

    def test_launch_waits_for_any_page_then_navigates(self):
        """Edge is launched on about:blank; the wait loop must accept the
        blank page target because open() performs the real navigation after
        mobile emulation is in place."""
        bridge = self.bridge_with(BridgeStatus.ENDPOINT_UNAVAILABLE, "offline")
        bridge.open.return_value = PageSnapshot(BridgeStatus.CONNECTED)
        version = {"Browser": "Edg/131", "webSocketDebuggerUrl": "ws://127.0.0.1:9311/devtools/browser/x"}
        targets = [{"type": "page", "url": "about:blank"}]

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        previous_port = app.ACTIVE_CDP_PORT
        with tempfile.TemporaryDirectory() as tmp:
            try:
                with patch.object(app, "school_bridge", return_value=bridge), \
                     patch.object(app, "find_free_cdp_port", return_value=9311), \
                     patch.object(app, "browser_profile", return_value=Path(tmp) / "profile"), \
                     patch.object(app.subprocess, "Popen") as popen, \
                     patch.object(app, "loopback_urlopen",
                                  side_effect=[FakeResponse(version), FakeResponse(targets)]):
                    result = app.open_school_h5()
            finally:
                app.ACTIVE_CDP_PORT = previous_port
        self.assertEqual(result["status"], "connected")
        bridge.open.assert_called_once_with()
        self.assertIn("about:blank", popen.call_args[0][0])
        self.assertNotIn(app.SCHOOL_HOME, popen.call_args[0][0])
        # The mobile identity must be process-level, not tied to a CDP session
        # that dies with the panel process.
        self.assertIn(f"--user-agent={app.MOBILE_USER_AGENT}", popen.call_args[0][0])

    def test_healthy_or_login_page_is_returned_without_launch(self):
        for status in (BridgeStatus.CONNECTED, BridgeStatus.LOGIN_REQUIRED):
            with self.subTest(status=status):
                bridge = self.bridge_with(status)
                with patch.object(app, "school_bridge", return_value=bridge), \
                     patch.object(app.subprocess, "Popen") as popen:
                    result = app.open_school_h5()
                self.assertEqual(result["status"], status.value)
                bridge.open.assert_not_called()
                popen.assert_not_called()


class EdgeDiscoveryTests(unittest.TestCase):
    def test_edge_candidates_always_include_well_known_defaults(self):
        """PROGRAMFILES(X86) can be redirected (this machine points it at D:
        while Edge lives on C:), so the default locations must still be probed."""
        with patch.dict(os.environ, {"PROGRAMFILES(X86)": "D:/moved/x86",
                                     "PROGRAMFILES": "D:/moved"}, clear=False):
            rendered = [str(path) for path in app.edge_candidates()]
        self.assertIn(str(Path("D:/moved/x86/Microsoft/Edge/Application/msedge.exe")), rendered)
        self.assertIn(str(Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")), rendered)
        self.assertEqual(len(rendered), len(set(rendered)))


class ReadAllRosterTests(unittest.TestCase):
    def test_read_all_roster_returns_records_and_counts(self):
        snapshot = SimpleNamespace(
            records=({"id": "1", "name": "测试甲", "status": "未打卡"},
                     {"id": "2", "name": "测试乙", "status": "已代确认"}),
            pending_count=1)
        runtime = Mock()
        runtime.chromium.connect_over_cdp.return_value = Mock(
            contexts=[Mock(pages=[Mock(url="https://zhxg.csu.edu.cn/znzhxgpt_h5/")])])
        runtime.stop = Mock()
        with patch.object(app, "BrowserBridge") as bridge_cls, \
             patch("playwright.sync_api.sync_playwright",
                   return_value=Mock(start=Mock(return_value=runtime))), \
             patch("official_adapter._read_board_from_ui", return_value=snapshot), \
             patch.object(app, "now", return_value=datetime(2026, 10, 25, 12, 0, tzinfo=app.TZ)):
            result = app.read_all_roster()
        bridge_cls.return_value.open.assert_called_once_with()
        self.assertEqual(result["counts"], {"未打卡": 1, "已打卡": 0, "已代确认": 1})
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual([row["id"] for row in result["records"]], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
