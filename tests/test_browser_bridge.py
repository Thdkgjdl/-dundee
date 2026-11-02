import json
import os
import socket
import sys
import unittest
from types import ModuleType
from unittest.mock import Mock, patch

import browser_bridge as bridge


class BrowserBridgeUnitTests(unittest.TestCase):
    def test_endpoint_must_be_loopback_and_port_bound(self):
        bridge.validate_endpoint("http://127.0.0.1:9239")
        with self.assertRaises(ValueError):
            bridge.validate_endpoint("http://example.com:9239")
        with self.assertRaises(ValueError):
            bridge.validate_endpoint("ws://127.0.0.1:9239")

    def test_environment_port_is_stable_and_does_not_spawn_browser(self):
        with patch.dict(os.environ, {"CSU_SAFETY_CDP_PORT": "9239"}, clear=False):
            config = bridge.BridgeConfig.from_environment()
        self.assertEqual(config.endpoint, "http://127.0.0.1:9239")
        with patch("browser_bridge.BrowserBridge._read_cdp", side_effect=bridge.BridgeError(bridge.BridgeStatus.ENDPOINT_UNAVAILABLE, "offline")), \
             patch("browser_bridge.BrowserBridge._read_playwright") as playwright, \
             patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = bridge.BrowserBridge(config).read_status()
        self.assertEqual(result.status, bridge.BridgeStatus.ENDPOINT_UNAVAILABLE)
        playwright.assert_not_called()

    def test_classifies_login_mobile_and_loaded_school_text(self):
        self.assertEqual(bridge._classify(bridge.SCHOOL_URL, "请使用手机访问"), bridge.BridgeStatus.MOBILE_REQUIRED)
        self.assertEqual(bridge._classify(bridge.SCHOOL_URL, "统一身份认证 请登录"), bridge.BridgeStatus.LOGIN_REQUIRED)
        self.assertEqual(bridge._classify(bridge.SCHOOL_URL, "安全待确认"), bridge.BridgeStatus.CONNECTED)
        self.assertEqual(bridge._classify("https://ids.csu.edu.cn/login", ""), bridge.BridgeStatus.LOGIN_REQUIRED)
        self.assertEqual(bridge._classify(bridge.SCHOOL_URL, ""), bridge.BridgeStatus.LOADING_FAILED)
        self.assertEqual(bridge._classify(bridge.SCHOOL_URL, "登录中 正在校验身份信息"), bridge.BridgeStatus.LOADING_FAILED)

    def test_snapshot_keeps_business_date_but_removes_url_parameters(self):
        snap = bridge._snapshot("打卡催办协助", bridge.SCHOOL_URL + "?rq=2026-10-13&ticket=secret",
                                "本班未打卡同学（0）", "playwright", True)
        self.assertEqual(snap.business_date, "2026-10-13")
        self.assertEqual(snap.business_route, "")
        self.assertNotIn("ticket", snap.url)
        self.assertNotIn("secret", snap.url)
        self.assertNotIn("?", snap.url)

    def test_snapshot_exposes_only_whitelisted_business_route(self):
        url = ("https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board"
               "?rq=2026-10-15&ticket=secret")
        snap = bridge._snapshot("班级打卡看板", url, "安全待确认", "playwright", True)
        self.assertEqual(snap.business_route, "/ywViews/qxjlfx/dqr/board")
        self.assertEqual(snap.business_date, "2026-10-15")
        self.assertNotIn("ticket", snap.as_dict()["url"])

        unknown = bridge._snapshot("H5", url.replace("/dqr/board", "/private/debug"), "", "playwright", True)
        self.assertEqual(unknown.business_route, "")

    def test_cdp_fallback_reads_only_visible_text_and_applies_mobile(self):
        class FakeSocket:
            def __init__(self):
                self.calls = []

            def send(self, raw):
                request = json.loads(raw)
                self.calls.append(request)
                method = request["method"]
                if method == "Target.attachToTarget":
                    result = {"sessionId": "s1"}
                elif method == "Runtime.evaluate":
                    result = {"result": {"value": json.dumps({"title": "安全待确认", "url": bridge.SCHOOL_URL, "text": "安全待确认"})}}
                else:
                    result = {}
                self.response = json.dumps({"id": request["id"], "result": result})

            def recv(self):
                return self.response

            def close(self):
                pass

        target = {"id": "p1", "type": "page", "url": bridge.SCHOOL_URL}
        fake = FakeSocket()
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}), \
             patch.object(bridge, "assert_cdp_reachable"), \
             patch.object(bridge.BrowserBridge, "_json_get", side_effect=[
                 {"webSocketDebuggerUrl": "ws://127.0.0.1:9239/devtools/browser/x"}, [target]]), \
             patch("websocket.create_connection", return_value=fake):
            result = bridge.BrowserBridge(bridge.BridgeConfig(endpoint="http://127.0.0.1:9239")).read()
        self.assertEqual(result.status, bridge.BridgeStatus.CONNECTED)
        self.assertEqual(result.text, "安全待确认")
        methods = [call["method"] for call in fake.calls]
        self.assertNotIn("Emulation.setDeviceMetricsOverride", methods)
        self.assertNotIn("Network.setUserAgentOverride", methods)
        self.assertNotIn("Input.dispatchMouseEvent", methods)
        self.assertNotIn("Page.navigate", methods)
        self.assertNotIn("Target.createTarget", methods)

    def test_refused_cdp_endpoint_reports_endpoint_unavailable(self):
        """A dead CDP port must not be misreported as a page-load failure.

        Playwright wraps a refused connection in its own error type; without
        the reachability guard the launcher would treat that as
        ``loading_failed`` and never start Edge.
        """
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        config = bridge.BridgeConfig(endpoint=f"http://127.0.0.1:{free_port}")
        result = bridge.BrowserBridge(config).read_status()
        self.assertEqual(result.status, bridge.BridgeStatus.ENDPOINT_UNAVAILABLE)

    def test_open_on_dead_endpoint_raises_endpoint_unavailable(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        config = bridge.BridgeConfig(endpoint=f"http://127.0.0.1:{free_port}")
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.BrowserBridge(config).open()
        self.assertEqual(caught.exception.status, bridge.BridgeStatus.ENDPOINT_UNAVAILABLE)

    def test_open_playwright_restarts_from_blank_before_mobile_gate_evaluation(self):
        """A school page booted under the desktop UA pins itself to the error
        page; open() must restart from about:blank so the SPA only ever
        evaluates its mobile gate on the emulated mobile client."""
        page = Mock()
        page.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/pages/public/error"
        page.title.return_value = "访问提示"
        stale = Mock()
        stale.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/pages/public/error"
        weixin = Mock()
        weixin.url = "https://open.weixin.qq.com/connect/qrconnect"
        context = Mock()
        context.pages = [page, stale, weixin]
        browser = Mock()
        browser.contexts = [context]
        runtime = Mock()
        runtime.chromium.connect_over_cdp.return_value = browser
        manager = Mock()
        manager.start.return_value = runtime
        package = ModuleType("playwright")
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(return_value=manager)
        sync_api.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})
        package.sync_api = sync_api
        with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": sync_api}), \
             patch.object(bridge, "assert_cdp_reachable"), \
             patch.object(bridge.BrowserBridge, "_find_playwright_page", return_value=page), \
             patch.object(bridge.BrowserBridge, "_prepare_mobile_playwright") as prepare:
            bridge.BrowserBridge(bridge.BridgeConfig(endpoint="http://127.0.0.1:9252")).open()
        prepare.assert_called_once()
        self.assertEqual([call.args[0] for call in page.goto.call_args_list],
                         ["about:blank", bridge.SCHOOL_URL])
        # Stale school tabs must not survive to confuse the read path, while
        # the navigated page and unrelated popups stay open.
        stale.close.assert_called_once_with()
        page.close.assert_not_called()
        weixin.close.assert_not_called()

    def test_close_stale_pages_only_drops_tool_owned_tabs(self):
        keep = Mock()
        school = Mock()
        school.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board"
        blank = Mock()
        blank.url = "about:blank"
        auth = Mock()
        auth.url = "https://ca.csu.edu.cn/authserver/login"
        weixin = Mock()
        weixin.url = "https://open.weixin.qq.com/connect/qrconnect"
        context = Mock()
        context.pages = [keep, school, blank, auth, weixin]
        bridge.BrowserBridge._close_stale_pages(context, keep)
        school.close.assert_called_once_with()
        blank.close.assert_called_once_with()
        auth.close.assert_called_once_with()
        weixin.close.assert_not_called()
        keep.close.assert_not_called()

    def test_open_playwright_emulates_auth_tab_before_login(self):
        """A restored authentication tab must be emulated before the user
        logs in, because the login redirect returns to the H5 on that tab."""
        page = Mock()
        page.url = "https://ca.csu.edu.cn/authserver/login?service=x"
        page.title.return_value = "统一身份认证平台"
        stale = Mock()
        stale.url = "about:blank"
        context = Mock()
        context.pages = [page, stale]
        browser = Mock()
        browser.contexts = [context]
        runtime = Mock()
        runtime.chromium.connect_over_cdp.return_value = browser
        manager = Mock()
        manager.start.return_value = runtime
        package = ModuleType("playwright")
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(return_value=manager)
        sync_api.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})
        package.sync_api = sync_api
        with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": sync_api}), \
             patch.object(bridge, "assert_cdp_reachable"), \
             patch.object(bridge.BrowserBridge, "_find_playwright_page", return_value=page), \
             patch.object(bridge.BrowserBridge, "_prepare_mobile_playwright") as prepare:
            result = bridge.BrowserBridge(bridge.BridgeConfig(endpoint="http://127.0.0.1:9252")).open()
        self.assertEqual(result.status, bridge.BridgeStatus.LOGIN_REQUIRED)
        prepare.assert_called_once()
        page.goto.assert_not_called()
        stale.close.assert_called_once_with()
        page.close.assert_not_called()

    def test_prepare_mobile_attaches_to_the_navigated_page(self):
        """The durable session must bind to the page Playwright navigates,
        not to a stale tab found by URL."""
        page = Mock()
        page.url = "about:blank"
        probe = Mock()
        probe.send.return_value = {"targetInfo": {"targetId": "T1"}}
        page.context.new_cdp_session.return_value = probe
        context = Mock()
        with patch.object(bridge.BrowserBridge, "_ensure_persistent_mobile_session",
                          return_value={}) as ensure:
            bridge.BrowserBridge(bridge.BridgeConfig(endpoint="http://127.0.0.1:9252"))._prepare_mobile_playwright(context, page)
        ensure.assert_called_once_with("about:blank", target_id="T1")
        probe.detach.assert_called_once_with()

    def test_persistent_session_prefers_the_supplied_target_id(self):
        class FakeSocket:
            def __init__(self):
                self.calls = []

            def send(self, raw):
                request = json.loads(raw)
                self.calls.append(request)
                method = request["method"]
                result = {"sessionId": "s1"} if method == "Target.attachToTarget" else {}
                self.response = json.dumps({"id": request["id"], "result": result})

            def recv(self):
                return self.response

            def close(self):
                pass

            @property
            def connected(self):
                return True

        config = bridge.BridgeConfig(endpoint="http://127.0.0.1:9252")
        stale = {"type": "page", "id": "STALE",
                 "url": "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/pages/public/error"}
        mine = {"type": "page", "id": "MINE", "url": "about:blank"}
        bridge._close_mobile_sessions()
        try:
            with patch.object(bridge.BrowserBridge, "_json_get", side_effect=[
                    {"webSocketDebuggerUrl": "ws://127.0.0.1:9252/devtools/browser/x"}, [stale, mine]]), \
                 patch("websocket.create_connection", return_value=FakeSocket()):
                session = bridge.BrowserBridge(config)._ensure_persistent_mobile_session(
                    "about:blank", target_id="MINE")
            self.assertEqual(session["target_id"], "MINE")
        finally:
            bridge._close_mobile_sessions()

    def test_mobile_session_is_reused_across_bridge_instances(self):
        class FakeSocket:
            def __init__(self):
                self.calls = []
                self.connected = True

            def send(self, raw):
                request = json.loads(raw)
                self.calls.append(request)
                if request["method"] == "Target.attachToTarget":
                    result = {"sessionId": "s1"}
                else:
                    result = {}
                self.response = json.dumps({"id": request["id"], "result": result})

            def recv(self):
                return self.response

            def close(self):
                self.connected = False

        fake = FakeSocket()
        target = {"id": "p1", "type": "page", "url": bridge.SCHOOL_URL}
        config = bridge.BridgeConfig(endpoint="http://127.0.0.1:9239")
        bridge._close_mobile_sessions()
        with patch.object(bridge.BrowserBridge, "_json_get", side_effect=[
                 {"webSocketDebuggerUrl": "ws://127.0.0.1:9239/devtools/browser/x"}, [target]]), \
             patch("websocket.create_connection", return_value=fake) as create:
            first = bridge.BrowserBridge(config)._ensure_persistent_mobile_session(bridge.SCHOOL_URL)
            second = bridge.BrowserBridge(config)._ensure_persistent_mobile_session(bridge.SCHOOL_URL)
        try:
            self.assertIs(first, second)
            self.assertEqual(create.call_count, 1)
            methods = [call["method"] for call in fake.calls]
            self.assertIn("Emulation.setDeviceMetricsOverride", methods)
            self.assertIn("Emulation.setTouchEmulationEnabled", methods)
            self.assertIn("Network.setUserAgentOverride", methods)
        finally:
            bridge._close_mobile_sessions()


if __name__ == "__main__":
    unittest.main()
