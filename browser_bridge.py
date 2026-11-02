"""Read-only bridge to an already running, local Edge CDP session.

The bridge deliberately does not start or stop a browser.  A caller supplies a
stable loopback CDP endpoint (normally the endpoint of the tool's independent
Edge profile).  Playwright is preferred when installed; the small CDP client
is only a dependency-free fallback for environments where Playwright is not
available.  No form fields, cookies, storage, or network responses are read.
"""
from __future__ import annotations

import json
import os
import re
import socket
import atexit
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlunsplit


SCHOOL_URL = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/pages/home/index"
SCHOOL_HOST = "zhxg.csu.edu.cn"
SCHOOL_PATH = "/znzhxgpt_h5/"
BUSINESS_ROUTES = frozenset({
    "/ywViews/qxjlfx/dqr/board",
    "/ywViews/qxjlfx/dqr/confirm",
    "/ywViews/qxjlfx/dqr/history",
    "/ywViews/qxjlfx/dqr/student",
    "/ywViews/qxjlfx/dkcb/assist",
})
DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9252"
DEFAULT_TEXT_LIMIT = 8000
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36 EdgA/131.0.0.0"
)

# Mobile emulation is target-scoped in Chromium.  A CDP session created from
# Playwright is released when Playwright disconnects, which also releases the
# emulation settings.  Keep a small, dedicated websocket alive for the life of
# this process so the independent Edge tab remains a mobile H5 tab.
_mobile_sessions: dict[str, dict[str, Any]] = {}
_mobile_sessions_lock = threading.RLock()


def _close_mobile_sessions() -> None:
    with _mobile_sessions_lock:
        sessions = list(_mobile_sessions.values())
        _mobile_sessions.clear()
    for session in sessions:
        try:
            session["connection"].close()
        except Exception:
            pass


atexit.register(_close_mobile_sessions)


class BridgeStatus(str, Enum):
    CONNECTED = "connected"
    LOGIN_REQUIRED = "login_required"
    MOBILE_REQUIRED = "mobile_required"
    LOADING_FAILED = "loading_failed"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    NOT_SCHOOL_PAGE = "not_school_page"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"


@dataclass(frozen=True)
class BridgeConfig:
    """Connection settings.  The endpoint is intentionally loopback-only."""

    endpoint: str = DEFAULT_CDP_ENDPOINT
    school_url: str = SCHOOL_URL
    mobile: bool = True
    text_limit: int = DEFAULT_TEXT_LIMIT
    navigation_timeout_ms: int = 15000
    profile_dir: Optional[str] = None
    origin: str = "http://127.0.0.1:8767"
    board_filter: str = "pending"

    @classmethod
    def from_environment(cls) -> "BridgeConfig":
        endpoint = os.environ.get("CSU_SAFETY_CDP_ENDPOINT", "").strip()
        if not endpoint:
            raw_port = os.environ.get("CSU_SAFETY_CDP_PORT", "9252").strip()
            try:
                port = int(raw_port)
            except ValueError as exc:
                raise ValueError("CSU_SAFETY_CDP_PORT 必须是端口号") from exc
            if not 1 <= port <= 65535:
                raise ValueError("CSU_SAFETY_CDP_PORT 超出范围")
            endpoint = f"http://127.0.0.1:{port}"
        profile = os.environ.get("CSU_SAFETY_EDGE_PROFILE") or None
        config = cls(endpoint=endpoint, profile_dir=profile,
                     origin=os.environ.get("CSU_SAFETY_PANEL_ORIGIN", "http://127.0.0.1:8767"))
        validate_endpoint(config.endpoint)
        return config


@dataclass(frozen=True)
class PageSnapshot:
    status: BridgeStatus
    title: str = ""
    url: str = ""
    text: str = ""
    detail: str = ""
    backend: str = ""
    mobile: bool = False
    business_date: str = ""
    business_route: str = ""
    board_filter: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "title": self.title,
            "url": self.url,
            "text": self.text,
            "detail": self.detail,
            "backend": self.backend,
            "mobile": self.mobile,
            "business_date": self.business_date,
            "business_route": self.business_route,
            "board_filter": self.board_filter,
        }


class BridgeError(RuntimeError):
    def __init__(self, status: BridgeStatus, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _is_school_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == SCHOOL_HOST and parsed.path.startswith(SCHOOL_PATH)


def _is_csu_auth_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname == "ca.csu.edu.cn" and
            parsed.path.startswith("/authserver/"))


def _safe_url(url: str) -> str:
    """Return only a trusted origin/path; never surface ticket or state query data."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".csu.edu.cn"):
        return ""
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _is_loopback(host: Optional[str]) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def validate_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not _is_loopback(parsed.hostname):
        raise ValueError("CDP 端点必须是本机 loopback HTTP 地址")
    if parsed.username or parsed.password or not parsed.port:
        raise ValueError("CDP 端点必须包含本机端口且不能带凭据")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("CDP 端点路径无效")


def assert_cdp_reachable(endpoint: str) -> None:
    """Fail fast when no browser listens on the CDP endpoint.

    Playwright wraps a refused connection in its own error type, which the
    read path would otherwise report as a page-load failure; the launcher
    needs a clean ``endpoint_unavailable`` to know it must start Edge.
    """
    parsed = urlparse(endpoint)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 0), timeout=1.0):
            pass
    except OSError as exc:
        raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE,
                          "独立 Edge 尚未连接，请先打开校园 H5 登录") from exc


def _loopback_ws_url(value: str, endpoint: str) -> str:
    parsed = urlparse(value)
    target = urlparse(endpoint)
    if parsed.scheme != "ws" or not _is_loopback(parsed.hostname) or parsed.port != target.port:
        raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE, "本机浏览器调试地址无效，已停止读取")
    return value


def _classify(url: str, text: str) -> BridgeStatus:
    if _is_csu_auth_url(url):
        return BridgeStatus.LOGIN_REQUIRED
    if not _is_school_url(url):
        return BridgeStatus.LOGIN_REQUIRED if "login" in url.lower() or "auth" in url.lower() else BridgeStatus.NOT_SCHOOL_PAGE
    if not text.strip():
        return BridgeStatus.LOADING_FAILED
    lower = text.lower()
    mobile_words = ("请使用手机访问", "请使用手机或微信打开", "请在手机端", "仅支持手机",
                    "仅支持在手机端", "移动端访问", "手机浏览", "微信中访问")
    login_words = ("统一身份认证", "请登录", "登录后", "用户名", "密码")
    error_words = ("无法访问此网站", "err_", "加载失败", "网络错误", "页面不存在", "登录中", "正在校验身份信息", "请稍候")
    if any(word in text or word.lower() in lower for word in mobile_words):
        return BridgeStatus.MOBILE_REQUIRED
    if any(word in text or word.lower() in lower for word in login_words):
        return BridgeStatus.LOGIN_REQUIRED
    if any(word in text or word.lower() in lower for word in error_words):
        return BridgeStatus.LOADING_FAILED
    return BridgeStatus.CONNECTED


def _board_filter(value: str) -> str:
    match = re.fullmatch(r"(全部|未打卡|已打卡|已代确认)[（(]\d+[）)]", value.strip())
    return {"全部": "all", "未打卡": "pending", "已打卡": "completed", "已代确认": "confirmed"}.get(match.group(1), "") if match else ""


def _snapshot(title: str, url: str, text: str, backend: str, mobile: bool, board_filter: str = "") -> PageSnapshot:
    fragment = urlparse(url).fragment
    fragment_path, _, fragment_query = fragment.partition("?")
    business_route = fragment_path if fragment_path in BUSINESS_ROUTES else ""
    requested = parse_qs(fragment_query).get("rq", [""])[0]
    business_date = requested if re.fullmatch(r"\d{4}-\d{2}-\d{2}", requested) else ""
    return PageSnapshot(_classify(url, text), title, _safe_url(url), text,
                        backend=backend, mobile=mobile, business_date=business_date,
                        business_route=business_route,
                        board_filter=board_filter if business_route == "/ywViews/qxjlfx/dqr/board" else "")


class BrowserBridge:
    """Connect to an independent Edge session and inspect one H5 page."""

    def __init__(self, config: Optional[BridgeConfig] = None):
        self.config = config or BridgeConfig.from_environment()
        validate_endpoint(self.config.endpoint)

    def read(self) -> PageSnapshot:
        """Read the school page, preferring Playwright and falling back to CDP."""
        assert_cdp_reachable(self.config.endpoint)
        try:
            import playwright.sync_api  # type: ignore  # noqa: F401
        except ImportError:
            try:
                return self._read_cdp()
            except BridgeError:
                raise
            except Exception as exc:
                raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE, "本机 CDP 页面读取失败") from exc
        try:
            return self._read_playwright()
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(BridgeStatus.LOADING_FAILED, "Playwright 页面读取失败，请确认 H5 已加载") from exc

    def open(self) -> PageSnapshot:
        """Prepare the existing session for the official H5 URL.

        This is the only method that may create a tab or navigate an existing
        blank tab.  It never navigates an authentication tab and never closes
        the connected browser.
        """
        assert_cdp_reachable(self.config.endpoint)
        try:
            import playwright.sync_api  # type: ignore  # noqa: F401
        except ImportError:
            return self._open_cdp()
        return self._open_playwright()

    def read_status(self) -> PageSnapshot:
        """Return a structured failure status for a panel/API caller."""
        try:
            return self.read()
        except BridgeError as exc:
            return PageSnapshot(exc.status, detail=exc.detail)

    def _read_playwright(self) -> PageSnapshot:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore

        runtime = sync_playwright().start()
        browser = None
        try:
            browser = runtime.chromium.connect_over_cdp(
                self.config.endpoint, timeout=self.config.navigation_timeout_ms,
            )
            contexts = browser.contexts
            if not contexts:
                raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE, "CDP 浏览器没有可用页面上下文")
            context = contexts[0]
            page = self._find_playwright_page(context)
            if page is None:
                raise BridgeError(BridgeStatus.LOADING_FAILED, "尚未打开学校 H5，请先执行打开连接")
            if _is_csu_auth_url(page.url):
                return PageSnapshot(BridgeStatus.LOGIN_REQUIRED, title=page.title(), url=_safe_url(page.url), backend="playwright", mobile=False)
            if not _is_school_url(page.url):
                if page.url.startswith("about:blank"):
                    raise BridgeError(BridgeStatus.LOADING_FAILED, "学校 H5 尚未导航，请先执行打开连接")
                else:
                    return PageSnapshot(BridgeStatus.NOT_SCHOOL_PAGE, title=page.title(), url=_safe_url(page.url), backend="playwright", mobile=self.config.mobile)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=self.config.navigation_timeout_ms)
                title = page.title()
                if _is_csu_auth_url(page.url):
                    return PageSnapshot(BridgeStatus.LOGIN_REQUIRED, title=title, url=_safe_url(page.url), backend="playwright", mobile=self.config.mobile)
                if not _is_school_url(page.url):
                    return PageSnapshot(BridgeStatus.NOT_SCHOOL_PAGE, title=title, url=_safe_url(page.url), backend="playwright", mobile=self.config.mobile)
                text = page.locator("body").inner_text(timeout=self.config.navigation_timeout_ms)[: self.config.text_limit]
                selected = page.locator(".padk-tabs--board .padk-tab--on").all_inner_texts() if urlparse(page.url).fragment.partition("?")[0] == "/ywViews/qxjlfx/dqr/board" else []
                board_filter = _board_filter(selected[0]) if len(selected) == 1 else ""
            except PlaywrightTimeoutError as exc:
                raise BridgeError(BridgeStatus.LOADING_FAILED, "学校 H5 页面加载超时") from exc
            return _snapshot(title, page.url, text, "playwright", self.config.mobile, board_filter)
        finally:
            # Disconnect only.  browser.close() would close the user's independent Edge.
            try:
                runtime.stop()
            except Exception:
                pass

    def _find_playwright_page(self, context: Any) -> Any:
        pages = list(context.pages)
        for page in pages:
            if _is_school_url(page.url):
                return page
        for page in pages:
            if _is_csu_auth_url(page.url):
                return page
        for page in pages:
            if page.url.startswith("about:blank"):
                return page
        return None

    @staticmethod
    def _page_target_id(page: Any) -> str:
        """Resolve the CDP target id of the exact page Playwright will use.

        The durable mobile session must attach to this page.  A URL-based
        guess can silently pick a stale restored tab while the page actually
        being navigated stays on the desktop user agent.
        """
        try:
            session = page.context.new_cdp_session(page)
        except Exception as exc:
            raise BridgeError(BridgeStatus.LOADING_FAILED, "无法建立页面调试会话") from exc
        try:
            info = session.send("Target.getTargetInfo")
            target_id = str((info or {}).get("targetInfo", {}).get("targetId", "")).strip()
        except Exception as exc:
            raise BridgeError(BridgeStatus.LOADING_FAILED, "无法读取页面调试目标") from exc
        finally:
            try:
                session.detach()
            except Exception:
                pass
        if not target_id:
            raise BridgeError(BridgeStatus.LOADING_FAILED, "页面调试目标为空")
        return target_id

    def _prepare_mobile_playwright(self, context: Any, page: Any) -> None:
        if not self.config.mobile:
            return
        # Keep this session independent from Playwright.  ``runtime.stop``
        # disconnects Playwright's temporary CDPSession and otherwise resets
        # the mobile emulation before the SPA has finished redirecting.
        try:
            self._ensure_persistent_mobile_session(page.url, target_id=self._page_target_id(page))
        except (BridgeError, ImportError):
            # Playwright remains a useful fallback when websocket-client is
            # unavailable; it still applies the settings for this navigation.
            session = context.new_cdp_session(page)
            session.send("Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 3,
                "mobile": True, "screenWidth": 390, "screenHeight": 844,
                "screenOrientation": {"type": "portraitPrimary", "angle": 0},
            })
            session.send("Network.setUserAgentOverride", {
                "userAgent": MOBILE_USER_AGENT, "platform": "Android",
            })

    def _ensure_persistent_mobile_session(self, target_url: str = "", *, target_id: Optional[str] = None,
                                          create_if_missing: bool = False) -> dict[str, Any]:
        """Attach one durable mobile CDP session to the selected page target."""
        if not self.config.mobile:
            return {}
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise BridgeError(BridgeStatus.DEPENDENCY_UNAVAILABLE, "缺少 websocket-client，无法保持移动页面") from exc

        endpoint = self.config.endpoint
        with _mobile_sessions_lock:
            current = _mobile_sessions.get(endpoint)
            if current is not None:
                connection = current.get("connection")
                connected = bool(getattr(connection, "connected", True))
                if connected:
                    return current
                try:
                    connection.close()
                except Exception:
                    pass
                _mobile_sessions.pop(endpoint, None)

            version = self._json_get("/json/version")
            targets = self._json_get("/json/list")
            target = None
            if target_id:
                target = next((item for item in targets
                               if item.get("type") == "page" and item.get("id") == target_id), None)
                if target is None:
                    raise BridgeError(BridgeStatus.LOADING_FAILED, "无法定位工具正在使用的学校页面目标")
            if target is None and target_url:
                target = next((item for item in targets
                               if item.get("type") == "page" and item.get("url") == target_url), None)
            if target is None:
                target = next((item for item in targets
                               if item.get("type") == "page" and _is_school_url(item.get("url", ""))), None)
            if target is None:
                target = next((item for item in targets
                               if item.get("type") == "page" and item.get("url", "").startswith("about:blank")), None)

            connection = websocket.create_connection(
                _loopback_ws_url(version.get("webSocketDebuggerUrl", ""), endpoint),
                timeout=5, origin=self.config.origin,
            )
            try:
                if target is None and create_if_missing:
                    result = self._command(connection, 1, "Target.createTarget", {"url": self.config.school_url})
                    target = {"id": result.get("targetId"), "url": self.config.school_url}
                if target is None or not target.get("id"):
                    raise BridgeError(BridgeStatus.LOADING_FAILED, "尚未打开学校 H5，请先执行打开连接")
                attached = self._command(connection, 2, "Target.attachToTarget", {"targetId": target["id"], "flatten": True})
                session_id = attached.get("sessionId")
                if not session_id:
                    raise BridgeError(BridgeStatus.LOADING_FAILED, "Edge 页面会话建立失败")
                command_id = 3
                self._command(connection, command_id, "Emulation.setDeviceMetricsOverride", {
                    "width": 390, "height": 844, "deviceScaleFactor": 3,
                    "mobile": True, "screenWidth": 390, "screenHeight": 844,
                    "screenOrientation": {"type": "portraitPrimary", "angle": 0},
                }, session_id)
                command_id += 1
                self._command(connection, command_id, "Emulation.setTouchEmulationEnabled", {
                    "enabled": True, "maxTouchPoints": 5,
                }, session_id)
                command_id += 1
                # Network is the broadly supported command; Emulation is
                # understood by newer Edge builds and keeps mobile=true.
                self._command(connection, command_id, "Network.setUserAgentOverride", {
                    "userAgent": MOBILE_USER_AGENT, "platform": "Android",
                }, session_id)
                command_id += 1
                try:
                    self._command(connection, command_id, "Emulation.setUserAgentOverride", {
                        "userAgent": MOBILE_USER_AGENT, "platform": "Android", "mobile": True,
                    }, session_id)
                except BridgeError:
                    pass
                current = {"connection": connection, "target_id": target["id"],
                           "session_id": session_id, "next_id": command_id + 1}
                _mobile_sessions[endpoint] = current
                return current
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass
                raise

    def _persistent_command(self, session: dict[str, Any], method: str,
                            params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with _mobile_sessions_lock:
            message_id = int(session.get("next_id", 100))
            session["next_id"] = message_id + 1
            try:
                return self._command(session["connection"], message_id, method, params, session["session_id"])
            except BridgeError:
                # Do not leave a dead websocket in the cache.  The next open
                # attempt will attach to the current browser instance.
                if _mobile_sessions.get(self.config.endpoint) is session:
                    _mobile_sessions.pop(self.config.endpoint, None)
                try:
                    session["connection"].close()
                except Exception:
                    pass
                raise

    @staticmethod
    def _close_stale_pages(context: Any, keep: Any) -> None:
        """Drop leftover school/auth/blank tabs in the tool's dedicated window.

        A restored browser session can reopen stale tabs (for example a page
        pinned to the desktop-only error page or an old authentication tab),
        and the read path would then keep selecting a stale tab instead of
        the one this tool navigated.  Only surfaces this tool itself selects
        are closed; unrelated popups such as a WeChat login window are left
        untouched.
        """
        for other in list(getattr(context, "pages", [])):
            if other is keep:
                continue
            url = str(getattr(other, "url", "") or "")
            if _is_school_url(url) or _is_csu_auth_url(url) or url.startswith("about:blank"):
                try:
                    other.close()
                except Exception:
                    pass

    def _open_playwright(self) -> PageSnapshot:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore
        runtime = sync_playwright().start()
        try:
            browser = runtime.chromium.connect_over_cdp(
                self.config.endpoint, timeout=self.config.navigation_timeout_ms,
            )
            if not browser.contexts:
                raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE, "CDP 浏览器没有可用页面上下文")
            context = browser.contexts[0]
            page = self._find_playwright_page(context)
            if page is None:
                page = context.new_page()
            if _is_csu_auth_url(page.url):
                # Emulate before the user logs in: the authentication redirect
                # returns to the H5 on this same tab, and a desktop-UA boot
                # would pin the session to the mobile-only error page.
                self._prepare_mobile_playwright(context, page)
                self._close_stale_pages(context, page)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                return PageSnapshot(BridgeStatus.LOGIN_REQUIRED, title=page.title(), url=_safe_url(page.url), backend="playwright", mobile=False)
            self._prepare_mobile_playwright(context, page)
            if _is_school_url(str(getattr(page, "url", ""))):
                # A page opened with the school URL can boot the SPA before
                # emulation applies, and the app then pins itself to the
                # desktop-only error page.  Restart from a blank document so
                # the mobile gate is only ever evaluated on the mobile client.
                page.goto("about:blank", wait_until="domcontentloaded",
                          timeout=self.config.navigation_timeout_ms)
            page.goto(self.config.school_url, wait_until="domcontentloaded", timeout=self.config.navigation_timeout_ms)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=self.config.navigation_timeout_ms)
                self._close_stale_pages(context, page)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                if urlparse(self.config.school_url).fragment.partition("?")[0] == "/ywViews/qxjlfx/dqr/board":
                    self._select_board_tab(page)
                return PageSnapshot(BridgeStatus.CONNECTED if _is_school_url(page.url) else BridgeStatus.LOADING_FAILED,
                                    title=page.title(), url=_safe_url(page.url), backend="playwright", mobile=self.config.mobile)
            except PlaywrightTimeoutError as exc:
                raise BridgeError(BridgeStatus.LOADING_FAILED, "学校 H5 页面加载超时") from exc
        finally:
            try:
                runtime.stop()
            except Exception:
                pass

    _BOARD_TAB_LABELS = {"pending": "未打卡", "all": "全部"}

    def _select_board_tab(self, page: Any) -> None:
        """Click the board filter tab requested by ``board_filter``.

        The tab is located by its exact label and count badge, so a renamed or
        missing tab fails the read instead of silently leaving another view
        selected.
        """
        label = self._BOARD_TAB_LABELS.get(self.config.board_filter)
        if not label:
            return
        page.locator(".padk-tabs--board .padk-tab").filter(
            has_text=re.compile(rf"^{label}[（(]\d+[）)]$")
        ).click(timeout=self.config.navigation_timeout_ms)

    def _read_cdp(self) -> PageSnapshot:
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise BridgeError(BridgeStatus.DEPENDENCY_UNAVAILABLE, "缺少 Playwright 和 websocket-client，无法连接页面") from exc
        version = self._json_get("/json/version")
        targets = self._json_get("/json/list")
        target = next((item for item in targets if item.get("type") == "page" and _is_school_url(item.get("url", ""))), None)
        auth_target = next((item for item in targets if item.get("type") == "page" and _is_csu_auth_url(item.get("url", ""))), None)
        blank = next((item for item in targets if item.get("type") == "page" and item.get("url", "").startswith("about:blank")), None)
        if target is None and auth_target is not None:
            return PageSnapshot(BridgeStatus.LOGIN_REQUIRED, title=auth_target.get("title", ""),
                                url=_safe_url(auth_target.get("url", "")), backend="cdp")
        connection = websocket.create_connection(_loopback_ws_url(version.get("webSocketDebuggerUrl", ""), self.config.endpoint), timeout=5, origin=self.config.origin)
        try:
            if target is None:
                if auth_target is not None:
                    target = auth_target
                elif blank is not None:
                    target = blank
                else:
                    raise BridgeError(BridgeStatus.LOADING_FAILED, "尚未打开学校 H5，请先执行打开连接")
            session = self._command(connection, 2, "Target.attachToTarget", {"targetId": target["id"], "flatten": True})
            session_id = session.get("sessionId")
            command_id = 3
            meta_result = self._command(connection, command_id, "Runtime.evaluate", {
                "expression": "JSON.stringify({title:document.title,url:location.href})",
                "returnByValue": True, "awaitPromise": False}, session_id)
            command_id += 1
            meta_value = meta_result.get("result", {}).get("value")
            if not meta_value:
                raise BridgeError(BridgeStatus.LOADING_FAILED, "学校 H5 页面尚未完成渲染，请稍后重试")
            page = json.loads(meta_value)
            if _is_csu_auth_url(page.get("url", "")):
                return PageSnapshot(BridgeStatus.LOGIN_REQUIRED, title=page.get("title", ""), url=_safe_url(page.get("url", "")), backend="cdp", mobile=self.config.mobile)
            if not _is_school_url(page.get("url", "")):
                return PageSnapshot(BridgeStatus.NOT_SCHOOL_PAGE, title=page.get("title", ""), url=_safe_url(page.get("url", "")), backend="cdp", mobile=self.config.mobile)
            text_result = self._command(connection, command_id, "Runtime.evaluate", {
                "expression": "(document.body?.innerText||'').slice(0,8000)",
                "returnByValue": True, "awaitPromise": False}, session_id)
            text = text_result.get("result", {}).get("value", "")
            if isinstance(text, str) and text.startswith("{"):
                # Tolerate runtimes that return the metadata envelope for a
                # second evaluation while still exposing visible text only.
                try:
                    text = json.loads(text).get("text", "")
                except ValueError:
                    pass
            selected_filter = ""
            if urlparse(page["url"]).fragment.partition("?")[0] == "/ywViews/qxjlfx/dqr/board":
                filter_result = self._command(connection, command_id + 1, "Runtime.evaluate", {
                    "expression": "JSON.stringify(Array.from(document.querySelectorAll('.padk-tabs--board .padk-tab--on')).map(e=>e.innerText))",
                    "returnByValue": True, "awaitPromise": False}, session_id)
                selected = json.loads(filter_result.get("result", {}).get("value", "[]"))
                selected_filter = _board_filter(selected[0]) if len(selected) == 1 else ""
            return _snapshot(page.get("title", ""), page["url"], text[:self.config.text_limit], "cdp", self.config.mobile, selected_filter)
        finally:
            connection.close()

    def _open_cdp(self) -> PageSnapshot:
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise BridgeError(BridgeStatus.DEPENDENCY_UNAVAILABLE, "缺少 Playwright 和 websocket-client，无法连接页面") from exc
        version = self._json_get("/json/version")
        targets = self._json_get("/json/list")
        target = next((item for item in targets if item.get("type") == "page" and _is_school_url(item.get("url", ""))), None)
        auth_target = next((item for item in targets if item.get("type") == "page" and _is_csu_auth_url(item.get("url", ""))), None)
        blank = next((item for item in targets if item.get("type") == "page" and item.get("url", "").startswith("about:blank")), None)
        if target is None and auth_target is not None:
            if self.config.mobile:
                # Same reason as the Playwright path: the login redirect
                # returns to the H5 on this tab, so it must already be mobile.
                try:
                    self._ensure_persistent_mobile_session(auth_target.get("url", ""), create_if_missing=False)
                except BridgeError:
                    pass
            return PageSnapshot(BridgeStatus.LOGIN_REQUIRED, url=_safe_url(auth_target.get("url", "")), backend="cdp", mobile=False)
        if not self.config.mobile:
            # Non-mobile mode is retained for diagnostics and tests.  It uses
            # a short-lived CDP connection because there is no emulation state
            # that needs to survive the call.
            connection = websocket.create_connection(
                _loopback_ws_url(version.get("webSocketDebuggerUrl", ""), self.config.endpoint),
                timeout=5, origin=self.config.origin,
            )
            try:
                if target is None:
                    if blank is not None:
                        target = blank
                    else:
                        result = self._command(connection, 1, "Target.createTarget", {"url": self.config.school_url})
                        target = {"id": result.get("targetId"), "url": self.config.school_url}
                session = self._command(connection, 2, "Target.attachToTarget", {"targetId": target["id"], "flatten": True})
                self._command(connection, 3, "Page.navigate", {"url": self.config.school_url}, session.get("sessionId"))
                return PageSnapshot(BridgeStatus.CONNECTED, url=self.config.school_url, backend="cdp", mobile=False)
            finally:
                connection.close()
        # The durable session owns its websocket and remains attached while
        # the H5 SPA redirects and renders.  Closing it here would reset the
        # mobile UA and make the service report "请使用手机访问" again.
        session = self._ensure_persistent_mobile_session(
            (target or blank or {}).get("url", self.config.school_url),
            create_if_missing=target is None,
        )
        self._persistent_command(session, "Page.navigate", {"url": self.config.school_url})
        return PageSnapshot(BridgeStatus.CONNECTED, url=self.config.school_url, backend="cdp", mobile=self.config.mobile)

    def _json_get(self, path: str) -> Any:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.config.endpoint.rstrip("/") + path, timeout=2) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE, "校园 H5 浏览器未连接，请先启动独立 Edge") from exc

    @staticmethod
    def _command(connection: Any, message_id: int, method: str, params: Optional[dict[str, Any]] = None, session_id: Optional[str] = None) -> dict[str, Any]:
        request: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            request["sessionId"] = session_id
        connection.send(json.dumps(request))
        deadline = time.monotonic() + 10
        while True:
            if time.monotonic() >= deadline:
                raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE, "Edge 页面响应超时")
            try:
                message = json.loads(connection.recv())
            except BridgeError:
                raise
            except Exception as exc:
                raise BridgeError(BridgeStatus.ENDPOINT_UNAVAILABLE, "Edge 页面连接中断") from exc
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise BridgeError(BridgeStatus.LOADING_FAILED, "Edge 页面读取失败")
            return message.get("result", {})

