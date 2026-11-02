"""Conservative Playwright-only adapter for official H5 confirmations.

This module deliberately has no API, CDP, cookie, or credential access.  It
operates on a caller-supplied Playwright ``Page`` and stops at the first
ambiguous state.  Callers must explicitly authorize the supervised trial or
an already verified batch before using its submission functions.
"""
from __future__ import annotations

import re
import time as monotonic_time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from roster_parser import (SAFETY_COUNT_RE, SAFETY_FILTER_RE, SAFETY_NOTICE_RE,
                           SAFETY_TITLE_RE, parse_safety_board_text)


SCHOOL_HOST = "zhxg.csu.edu.cn"
BOARD_ROUTE = "/ywViews/qxjlfx/dqr/board"
DEFAULT_BOARD_URL = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board"
METHODS = frozenset({"电话", "当面", "室友转达"})
CONFIRMED_STATUS = "已代确认"
PENDING_STATUS = "未打卡"


class AdapterError(RuntimeError):
    """A pre-submit safety check failed; no form submission was attempted."""


@dataclass(frozen=True)
class BoardSnapshot:
    date: str
    route: str
    title: str
    records: tuple[dict[str, str], ...]
    pending_count: int
    selected_filter: str = ""

    @property
    def confirmed_ids(self) -> frozenset[str]:
        return frozenset(row["id"] for row in self.records if row["status"] == CONFIRMED_STATUS)


@dataclass
class TrialConfig:
    """Dependencies for a supervised trial.

    ``page`` must be a Playwright Page in the same thread that calls
    ``execute_trial``.  ``read_board`` is a read-only closure supplied by the
    caller when it already has a stricter page snapshot/parser.  When omitted,
    the adapter uses the visible board text and card DOM for its checks.
    """

    page: Any
    supervised_approved: bool = False
    board_url: str = DEFAULT_BOARD_URL
    read_board: Optional[Callable[[Any], BoardSnapshot | dict[str, Any]]] = None
    board_filter: str = "all"
    now: Callable[[], datetime] = lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
    navigation_timeout_ms: int = 15000


@dataclass(frozen=True)
class TrialResult:
    status: str
    detail: str
    submitted: bool = False
    record_id: str = ""
    pending_before: Optional[int] = None
    pending_after: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "submitted": self.submitted,
            "record_id": self.record_id,
            "pending_before": self.pending_before,
            "pending_after": self.pending_after,
        }


@dataclass(frozen=True)
class BatchResult:
    status: str
    detail: str
    submitted_count: int
    total_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "submitted_count": self.submitted_count,
            "total_count": self.total_count,
        }


def _today(config: TrialConfig) -> str:
    value = config.now()
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise AdapterError("时钟必须返回 Asia/Shanghai 日期")


def _route(url: str) -> str:
    fragment = urlparse(url or "").fragment
    return fragment.partition("?")[0]


def _is_school_h5_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return (parsed.scheme == "https" and parsed.hostname == SCHOOL_HOST and
            parsed.path.startswith("/znzhxgpt_h5/"))


def _value(record: Any, key: str, default: str = "") -> str:
    if isinstance(record, dict):
        value = record.get(key, default)
    else:
        value = getattr(record, key, default)
    return str(value).strip()


def _records(records: Iterable[Any], *, default_status: str = PENDING_STATUS) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in records:
        ident = _value(row, "id")
        name = _value(row, "name")
        status = _value(row, "status", default_status) or default_status
        if not ident or not name or ident in seen:
            raise AdapterError("名单包含缺失或重复学号")
        seen.add(ident)
        result.append({"id": ident, "name": name, "status": status})
    return tuple(sorted(result, key=lambda item: item["id"]))


def _coerce_snapshot(value: BoardSnapshot | dict[str, Any], expected_date: str) -> BoardSnapshot:
    if isinstance(value, BoardSnapshot):
        snapshot = value
    elif isinstance(value, dict):
        snapshot = BoardSnapshot(
            date=str(value.get("date", value.get("business_date", ""))),
            route=str(value.get("route", value.get("business_route", ""))),
            title=str(value.get("title", "")),
            records=_records(value.get("records", value.get("roster", ())), default_status=""),
            pending_count=int(value.get("pending_count", 0)),
            selected_filter=str(value.get("selected_filter", value.get("filter", ""))),
        )
    else:
        raise AdapterError("名单快照格式无效")
    if snapshot.date != expected_date or snapshot.route != BOARD_ROUTE or snapshot.title != "班级打卡看板":
        raise AdapterError("官方看板日期或路由无法核验")
    if snapshot.pending_count < 0:
        raise AdapterError("官方看板人数无效")
    return BoardSnapshot(snapshot.date, snapshot.route, snapshot.title,
                         _records(snapshot.records, default_status=""), snapshot.pending_count,
                         snapshot.selected_filter)


def _active_board_filter(page: Any) -> str:
    tabs = page.locator(".padk-tabs--board .padk-tab--on")
    if tabs.count() != 1:
        raise AdapterError("无法唯一读取官方看板当前筛选")
    label = str(tabs.inner_text(timeout=15000)).strip()
    if label.startswith("全部"):
        return "all"
    if label.startswith("未打卡"):
        return "pending"
    raise AdapterError("官方看板当前筛选不是全部或未打卡")


def _select_board_filter(page: Any, selected_filter: str) -> None:
    """Use an observed official tab and verify the resulting active tab."""
    labels = {"all": "全部", "pending": "未打卡"}
    if selected_filter not in labels:
        raise AdapterError("不支持的官方看板筛选")
    label = labels[selected_filter]
    tabs = page.locator(".padk-tabs--board .padk-tab").filter(
        has_text=re.compile(rf"^{label}\s*[（(]\d{{1,4}}\s*[）)]$")
    )
    if tabs.count() != 1:
        raise AdapterError("无法唯一定位官方看板筛选")
    tabs.click()
    if _active_board_filter(page) != selected_filter:
        raise AdapterError("官方看板筛选未生效")


def _select_all_board_filter(page: Any) -> None:
    _select_board_filter(page, "all")


def _visible_body(page: Any) -> str:
    return str(page.locator("body").inner_text(timeout=15000))


def _read_board_from_ui(page: Any, expected_date: str, selected_filter: str) -> BoardSnapshot:
    """Read only visible board DOM; no hidden state or network responses."""
    url = str(getattr(page, "url", ""))
    parsed_url = urlparse(url)
    if (parsed_url.scheme != "https" or parsed_url.hostname != SCHOOL_HOST or
            not parsed_url.path.startswith("/znzhxgpt_h5/")):
        raise AdapterError("当前页面不是受信任的学校 H5")
    route = _route(url)
    title = str(page.title())
    if title != "班级打卡看板":
        raise AdapterError("页面标题不是班级打卡看板")
    body = _visible_body(page)
    if route != BOARD_ROUTE:
        raise AdapterError("当前页面不是官方班级打卡看板")
    actual_filter = _active_board_filter(page)
    if selected_filter and selected_filter != actual_filter:
        raise AdapterError("官方看板筛选与预期不一致")
    selected_filter = actual_filter

    # Before a submission the strict parser proves that the complete roster is
    # still pending.  After a submission it intentionally rejects mixed rows,
    # so the card reader below is used to inspect the resulting statuses.
    parsed = parse_safety_board_text(body, expected_date, expected_date,
                                      selected_filter=selected_filter)
    if parsed.ok:
        rows = _records(parsed.records)
        return BoardSnapshot(expected_date, route, title, rows, len(rows), selected_filter)

    # A mixed ``全部`` board is expected after one confirmation.  It may be
    # inspected only through the observed card classes below.  A pending view
    # that fails the strict parser is never guessed from arbitrary text.
    if selected_filter != "all":
        raise AdapterError("官方看板未通过完整名单解析")

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or not SAFETY_TITLE_RE.fullmatch(lines[0]):
        raise AdapterError("看板业务标题无法核验")
    header_index = 1
    if header_index < len(lines) and lines[header_index] == "查看历史":
        header_index += 1
    summary: dict[str, int] = {}
    for label in ("应打卡", "已打卡", "已代确认", "未打卡"):
        if (header_index + 1 >= len(lines) or lines[header_index] != label or
                not SAFETY_COUNT_RE.fullmatch(lines[header_index + 1])):
            raise AdapterError("看板汇总结构无法核验")
        summary[label] = int(lines[header_index + 1])
        header_index += 2
    if header_index >= len(lines) or not SAFETY_NOTICE_RE.fullmatch(lines[header_index]):
        raise AdapterError("看板安全代确认说明缺失")
    header_index += 1
    filters: dict[str, int] = {}
    for label in ("全部", "未打卡", "已打卡", "已代确认"):
        if header_index >= len(lines):
            raise AdapterError("看板筛选计数无法核验")
        match = SAFETY_FILTER_RE.fullmatch(lines[header_index])
        if match is None or match.group("label") != label:
            raise AdapterError("看板筛选计数无法核验")
        filters[label] = int(match.group("count"))
        header_index += 1
    if (summary["应打卡"] != filters["全部"] or
            summary["已打卡"] + summary["已代确认"] + summary["未打卡"] != summary["应打卡"] or
            filters["未打卡"] != summary["未打卡"] or
            filters["已打卡"] != summary["已打卡"] or
            filters["已代确认"] != summary["已代确认"]):
        raise AdapterError("看板汇总与筛选计数不一致")

    cards = page.locator(".padk-person")
    count = cards.count()
    if count != filters["全部"]:
        raise AdapterError("看板卡片数量与全部人数不一致")
    rows: list[dict[str, str]] = []
    for index in range(count):
        card = cards.nth(index)
        name_field = card.locator(".padk-person-name")
        chip_field = card.locator(".padk-chip")
        sub_field = card.locator(".padk-person-sub")
        if name_field.count() != 1 or chip_field.count() != 1 or sub_field.count() != 1:
            raise AdapterError("看板卡片字段不唯一")
        name = str(name_field.inner_text(timeout=15000)).strip()
        status = str(chip_field.inner_text(timeout=15000)).strip()
        sub = str(sub_field.inner_text(timeout=15000)).strip()
        if status not in {"未打卡", "已代确认", "已打卡"}:
            raise AdapterError("看板卡片状态无法核验")
        # A checked-in card appends its check-in time to the sub line
        # (``学号 <id> · 20:09 已打卡``).  Only the leading 学号 identifies the
        # student, so the trailing detail must not fail the parse.
        match = re.match(r"学号\s*[:：]?\s*([A-Za-z0-9-]{4,32})", sub)
        if not name or not match:
            raise AdapterError("看板卡片姓名或学号无法核验")
        ident = match.group(1)
        rows.append({"id": ident, "name": name, "status": status})
    normalized = _records(rows, default_status="")
    if any(sum(row["status"] == status for row in normalized) != summary[status]
           for status in ("未打卡", "已代确认", "已打卡")):
        raise AdapterError("看板卡片状态与汇总人数不一致")
    return BoardSnapshot(expected_date, route, title, normalized,
                         sum(row["status"] == PENDING_STATUS for row in normalized), selected_filter)


def _read_board(config: TrialConfig, expected_date: str, selected_filter: Optional[str] = None) -> BoardSnapshot:
    if config.read_board is not None:
        return _coerce_snapshot(config.read_board(config.page), expected_date)
    return _read_board_from_ui(config.page, expected_date, selected_filter or "")


def _same_roster(left: Iterable[dict[str, str]], right: Iterable[dict[str, str]]) -> bool:
    return tuple(sorted(left, key=lambda row: row["id"])) == tuple(sorted(right, key=lambda row: row["id"]))


def _unknown(record_id: str, detail: str, pending_before: Optional[int], *, submitted: bool = False) -> TrialResult:
    return TrialResult("unknown", detail, submitted=submitted, record_id=record_id,
                       pending_before=pending_before)


def _wait_for_route(page: Any, route: str, timeout_ms: int) -> None:
    """Wait until the SPA hash router reaches the expected route.

    ``uni.navigateTo`` changes the URL synchronously but mounts the lazily
    loaded page chunk afterwards, so callers must not treat the URL change
    alone as proof that the page is ready.
    """
    deadline = monotonic_time.monotonic() + max(timeout_ms, 0) / 1000
    while _route(str(getattr(page, "url", ""))) != route:
        if monotonic_time.monotonic() >= deadline:
            return
        page.wait_for_timeout(100)


def _wait_for_form_text(page: Any, pattern: "re.Pattern[str]", timeout_ms: int) -> str:
    """Read the visible body until the official form renders expected text.

    The confirm page is a lazily loaded SPA route: its URL and document title
    change before the chunk mounts, and the board page is hidden during the
    transition, so one immediate read can observe a body containing neither
    page.  The last observed body is returned either way so the caller's
    strict checks still report a precise failure when the form never settles.
    """
    deadline = monotonic_time.monotonic() + max(timeout_ms, 0) / 1000
    body = ""
    while True:
        body = _visible_body(page)
        if pattern.search(body) or monotonic_time.monotonic() >= deadline:
            return body
        page.wait_for_timeout(100)


def execute_trial(config: TrialConfig, record: Any, method: str, explanation: str,
                  expected_roster: Iterable[Any], expected_date: str) -> TrialResult:
    """Submit exactly one supervised record through the visible official UI."""
    if config.supervised_approved is not True:
        raise AdapterError("试运行未获明确监督授权，已锁定")
    if not isinstance(expected_date, str) or expected_date != _today(config):
        raise AdapterError("仅允许操作 Asia/Shanghai 当天名单")
    if method not in METHODS:
        raise AdapterError("核实方式必须是电话、当面或室友转达")
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation.strip()) > 100:
        raise AdapterError("情况说明不能为空且不得超过 100 字")
    expected = _records(expected_roster)
    target = _records([record])[0]
    if target not in expected:
        raise AdapterError("目标记录不在已核验官方名单中")

    before = _read_board(config, expected_date)
    if before.selected_filter not in {"all", "pending"}:
        raise AdapterError("提交前看板筛选状态未核验")
    pending_rows = tuple(row for row in before.records if row["status"] == PENDING_STATUS)
    if not _same_roster(pending_rows, expected) or before.pending_count != len(expected):
        raise AdapterError("提交前官方名单与已核验快照不一致，已停止")

    # Keep a complete class baseline, including students who already checked
    # in.  Only the selected target may change when the form is submitted.
    baseline = before
    if config.read_board is None and before.selected_filter == "pending":
        _select_all_board_filter(config.page)
        baseline = _read_board(config, expected_date, "all")
        baseline_pending = tuple(row for row in baseline.records if row["status"] == PENDING_STATUS)
        if not _same_roster(baseline_pending, expected) or baseline.pending_count != len(expected):
            raise AdapterError("全部看板与已核验待确认名单不一致，已停止")
        _select_board_filter(config.page, "pending")
        visible_pending = _read_board(config, expected_date, "pending")
        if (not _same_roster(visible_pending.records, expected) or
                visible_pending.pending_count != len(expected)):
            raise AdapterError("切换筛选后待确认名单发生变化，已停止")

    page = config.page
    # Strict card/action lookup: a student number must identify exactly one
    # card, and that card must expose exactly one official action.
    student = page.get_by_text(f"学号 {target['id']}", exact=True)
    cards = page.locator(".padk-person").filter(has=student)
    # The live card renders ``姓名 · 学号 <id>`` as one text node, so the
    # exact student locator above may be empty.  A full-line regex is the
    # fallback; it still binds the action to exactly one card and never uses a
    # substring match that could select a different student.
    if cards.count() != 1:
        cards = page.locator(".padk-person").filter(
            has_text=re.compile(rf"(?m)^.*学号\s*[:：]?\s*{re.escape(target['id'])}\s*$"))
    if cards.count() != 1:
        raise AdapterError("无法唯一定位目标学生卡片")
    card = cards.nth(0)
    actions = card.get_by_text("安全代确认", exact=True)
    if actions.count() != 1:
        raise AdapterError("目标卡片缺少唯一安全代确认按钮")
    actions.click()

    submitted = False
    try:
        # The confirm page is a lazily loaded SPA route.  Wait for the route
        # and for the rendered form before the strict checks below decide the
        # outcome; a single immediate read can observe a body that contains
        # neither the hidden board nor the not-yet-mounted form.
        id_pattern = re.compile(rf"(?m)^.*学号\s*[:：]?\s*{re.escape(target['id'])}\s*$")
        _wait_for_route(page, "/ywViews/qxjlfx/dqr/confirm", config.navigation_timeout_ms)
        body = _wait_for_form_text(page, id_pattern, config.navigation_timeout_ms)
        title = str(page.title())
        if not _is_school_h5_url(str(getattr(page, "url", ""))) or _route(str(getattr(page, "url", ""))) != "/ywViews/qxjlfx/dqr/confirm":
            return _unknown(target["id"], "未进入官方安全代确认表单", before.pending_count)
        if title != "安全代确认":
            return _unknown(target["id"], "打开后未确认安全代确认表单", before.pending_count)
        if id_pattern.search(body) is None:
            return _unknown(target["id"], "安全代确认表单未显示目标学号", before.pending_count)

        # The official form renders each method as a ``.radio-item`` row whose
        # label is a uni-app ``<uni-text>`` custom element, so the option is
        # located by its row class and exact label rather than by tag name.
        method_field = page.locator(".radio-item").filter(
            has_text=re.compile(rf"^{re.escape(method)}$"))
        if method_field.count() != 1:
            return _unknown(target["id"], "核实方式选项不唯一或不可见", before.pending_count)
        method_field.click()
        textarea = page.locator("textarea.uni-textarea-textarea")
        if textarea.count() != 1:
            return _unknown(target["id"], "情况说明输入框不唯一", before.pending_count)
        textarea.fill(explanation.strip())
        submit = page.locator(".padk-bar-btn--primary").filter(has_text="提交代确认")
        if submit.count() != 1:
            return _unknown(target["id"], "提交按钮不唯一或不可见", before.pending_count)
        if _today(config) != expected_date:
            raise AdapterError("提交前日期已变化，已停止")
        submitted = True
        # Mark before invoking click: once the official submit gesture starts,
        # a browser timeout makes the outcome unknown and must never be retried.
        submit.click()

        # The submit handler may still be committing navigation.  Wait for the
        # official board route; a timeout is an unknown outcome and is never
        # recovered by replaying the submit gesture.
        board_pattern = re.compile(r"^https://zhxg\.csu\.edu\.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board(?:\?.*)?$")
        page.wait_for_url(board_pattern, timeout=config.navigation_timeout_ms)
        if _route(str(getattr(page, "url", ""))) != BOARD_ROUTE:
            return _unknown(target["id"], "提交后未回到官方班级打卡看板", before.pending_count,
                            submitted=True)
        if config.read_board is None:
            _select_all_board_filter(page)
        after = _read_board(config, expected_date, "all")
    except Exception as exc:
        if submitted:
            return _unknown(target["id"], f"提交结果无法核验：{type(exc).__name__}", before.pending_count,
                            submitted=True)
        raise AdapterError(f"提交前表单校验失败：{type(exc).__name__}") from exc

    if after.selected_filter != "all":
        return _unknown(target["id"], "提交后未确认全部筛选视图", before.pending_count,
                        submitted=True)
    expected_ids = {row["id"] for row in baseline.records}
    observed_ids = {row["id"] for row in after.records}
    if observed_ids != expected_ids:
        return _unknown(target["id"], "提交后未读取完整班级状态，已停止", before.pending_count,
                        submitted=True)
    matches = [row for row in after.records if row["id"] == target["id"]]
    target_confirmed = len(matches) == 1 and matches[0]["status"] == CONFIRMED_STATUS
    before_by_id = {row["id"]: row for row in baseline.records}
    after_by_id = {row["id"]: row for row in after.records}
    others_unchanged = all(after_by_id[ident] == before_by_id[ident]
                           for ident in expected_ids if ident != target["id"])
    pending_after = after.pending_count
    target_unchanged_name = len(matches) == 1 and matches[0]["name"] == target["name"]
    if target_confirmed and target_unchanged_name and pending_after == before.pending_count - 1 and others_unchanged:
        return TrialResult("success", "目标记录已代确认且待确认人数减少 1", True,
                           target["id"], before.pending_count, pending_after)
    if target_confirmed or pending_after != before.pending_count:
        return TrialResult("partial", "提交后状态与人数未同时满足严格校验，已停止", True,
                           target["id"], before.pending_count, pending_after)
    return _unknown(target["id"], "提交后未观察到明确的已代确认状态", before.pending_count,
                    submitted=True)


def execute_batch(config: TrialConfig, plans: Iterable[Any], expected_roster: Iterable[Any],
                  expected_date: str, on_progress: Optional[Callable[[int], None]] = None) -> BatchResult:
    """Submit verified records sequentially, stopping after any uncertain result.

    The caller owns its persistent execution marker.  A successful progress
    callback means that one more record was independently verified on the
    official board; an attempted but uncertain submit is never counted or
    retried here.
    """
    if config.supervised_approved is not True:
        raise AdapterError("批次未获明确监督授权，已锁定")
    if not isinstance(expected_date, str) or expected_date != _today(config):
        raise AdapterError("仅允许操作 Asia/Shanghai 当天名单")
    expected = _records(expected_roster)
    if any(row["status"] != PENDING_STATUS for row in expected):
        raise AdapterError("已核验名单包含非待确认状态")
    plan_list = list(plans)
    if not plan_list:
        raise AdapterError("批次计划不能为空")
    by_id = {row["id"]: row for row in expected}
    seen: set[str] = set()
    validated: list[tuple[dict[str, str], str, str]] = []
    for plan in plan_list:
        ident = _value(plan, "id")
        method = _value(plan, "method")
        explanation = _value(plan, "explanation")
        if ident in seen or ident not in by_id:
            raise AdapterError("批次计划包含重复或未核验学号")
        if method not in METHODS:
            raise AdapterError("核实方式必须是电话、当面或室友转达")
        if not explanation or len(explanation) > 100:
            raise AdapterError("情况说明不能为空且不得超过 100 字")
        seen.add(ident)
        validated.append((by_id[ident], method, explanation))

    remaining = list(expected)
    submitted_count = 0
    total_count = len(validated)
    for record, method, explanation in validated:
        try:
            result = execute_trial(config, record, method, explanation, remaining, expected_date)
        except AdapterError as exc:
            # The adapter's own messages are fixed strings without personal
            # data, so they can safely explain the stop reason on disk.
            return BatchResult("unknown", f"本条校验或操作失败，批次已停止：{exc}",
                               submitted_count, total_count)
        except Exception as exc:
            return BatchResult("unknown", f"本条校验或操作失败，批次已停止：{type(exc).__name__}",
                               submitted_count, total_count)
        if result.status != "success":
            status = result.status if result.status in {"partial", "unknown"} else "unknown"
            return BatchResult(status, result.detail, submitted_count, total_count)
        submitted_count += 1
        remaining = [row for row in remaining if row["id"] != record["id"]]
        if on_progress is not None:
            try:
                on_progress(submitted_count)
            except Exception as exc:
                return BatchResult("unknown", f"进度保存失败，批次已停止：{type(exc).__name__}",
                                   submitted_count, total_count)
    return BatchResult("success", "批次各条记录均已通过官方看板结果校验", submitted_count, total_count)

