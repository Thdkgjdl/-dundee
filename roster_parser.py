"""Conservative, side-effect-free parser for an already-read H5 roster.

This module does not navigate, click, persist, or submit.  It intentionally
requires a trusted observed date, count summary, and one complete row per
person.  A page whose text does not expose that structure is rejected so it
cannot be mistaken for a different H5 workflow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional


DATE_RE = re.compile(r"(?:名单日期|打卡日期|日期)\s*[:：]?\s*(?P<date>20\d{2}-\d{2}-\d{2})")
COUNT_RE = re.compile(r"本班未打卡同学\s*[（(]\s*(?P<count>\d{1,3})\s*[）)]")
# The live page appends a fixed explanatory sentence to the numeric footer.
# Keep the suffix exact so arbitrary or truncated text cannot be mistaken for
# a complete roster boundary.
FOOTER_RE = re.compile(
    r"^本次已回填\s*(?P<filled>\d{1,3})\s*/\s*(?P<total>\d{1,3})\s*[。.]?"
    r"(?:\s*名单随打卡情况实时更新，\s*同学完成打卡后自动从本列表移除；\s*"
    r"你回填的情况会同步给辅导员参考。)?\s*$"
)
ID_RE = re.compile(r"^学号\s*[:：]?\s*(?P<id>[A-Za-z0-9-]{4,32})$")
NAME_RE = re.compile(r"^[^\s:：]{2,32}$")
SAFETY_TITLE_RE = re.compile(r"^.+\s·\s今日晚寝打卡$")
SAFETY_COUNT_RE = re.compile(r"^\d{1,4}$")
SAFETY_FILTER_RE = re.compile(r"^(?P<label>全部|未打卡|已打卡|已代确认)\s*[（(](?P<count>\d{1,4})\s*[）)]$")
SAFETY_NOTICE_RE = re.compile(
    r"^距今日打卡截止（[^）]*）还有.+?(?:点击|点)学生卡片可查看其打卡详情；"
    r"对确实无法本人打卡但已核实安全的同学，可发起「安全代确认」——"
    r"该记录不计为本人打卡，也不计缺勤。$"
)
SAFETY_EMPTY_LINES = {"本班同学都已完成打卡", "暂无数据", "暂无相关记录"}


@dataclass(frozen=True)
class RosterRecord:
    id: str
    name: str
    status: str = "未打卡"


@dataclass(frozen=True)
class RosterParseResult:
    ok: bool
    requested_date: str
    business_date: str = ""
    declared_count: Optional[int] = None
    records: tuple[RosterRecord, ...] = ()
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def audit_summary(self) -> str:
        """Safe audit text: never includes names or student numbers."""
        return f"名单解析{'成功' if self.ok else '失败'}；记录 {self.count} 条"


def parse_roster_text(text: str, requested_date: str, expected_count: Optional[int] = None,
                      observed_date: Optional[str] = None) -> RosterParseResult:
    """Parse one visible H5 text snapshot and fail closed on ambiguity."""
    try:
        requested = date.fromisoformat(requested_date)
    except (TypeError, ValueError):
        return RosterParseResult(False, str(requested_date), error="请求日期无效")
    if requested.isoformat() != requested_date:
        return RosterParseResult(False, requested_date, error="请求日期格式无效")
    if not isinstance(text, str) or not text.strip():
        return RosterParseResult(False, requested_date, error="页面正文为空")

    date_matches = DATE_RE.findall(text)
    if len(date_matches) > 1 and len(set(date_matches)) != 1:
        return RosterParseResult(False, requested_date, error="页面包含多个不一致日期")
    page_date = date_matches[0] if date_matches else observed_date
    if page_date != requested_date:
        return RosterParseResult(False, requested_date, business_date=page_date or "",
                                 error="页面日期缺失或与请求日期不一致")
    count_match = COUNT_RE.search(text)
    if not count_match:
        return RosterParseResult(False, requested_date, business_date=requested_date, error="未找到未打卡人数摘要")
    declared = int(count_match.group("count"))
    if expected_count is not None and declared != expected_count:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="页面人数与预期不一致")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header_index = next((index for index, line in enumerate(lines) if COUNT_RE.search(line)), None)
    if header_index is None:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="未找到名单起始位置")
    footer_index = next((index for index in range(header_index + 1, len(lines))
                         if FOOTER_RE.match(lines[index])), None)
    if footer_index is None:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="未找到名单结束摘要")
    footer = FOOTER_RE.match(lines[footer_index])
    assert footer is not None
    if int(footer.group("total")) != declared or int(footer.group("filled")) > declared:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="结束摘要人数不一致")
    body_lines = lines[header_index + 1:footer_index]
    if body_lines and body_lines[0] == "查看历史":
        body_lines = body_lines[1:]
    if declared == 0:
        if body_lines != ["本班同学都已完成打卡"]:
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="空名单状态不完整")
        return RosterParseResult(True, requested_date, business_date=requested_date,
                                 declared_count=declared, records=())
    records: list[RosterRecord] = []
    seen: set[str] = set()
    index = 0
    while index < len(body_lines):
        if (index + 3 >= len(body_lines) or not NAME_RE.match(body_lines[index]) or
                body_lines[index + 1] != "未打卡" or body_lines[index + 2] != "回填"):
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="名单行结构不完整")
        id_match = ID_RE.match(body_lines[index + 3])
        if not id_match:
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="名单缺少有效学号")
        ident = id_match.group("id")
        if ident in seen:
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="名单包含重复学号")
        seen.add(ident)
        records.append(RosterRecord(ident, body_lines[index], "未打卡"))
        index += 4

    if len(records) != declared:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="可识别记录数与页面摘要不一致")
    if expected_count is not None and len(records) != expected_count:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="完整名单人数与预期不一致")
    return RosterParseResult(True, requested_date, business_date=requested_date,
                             declared_count=declared, records=tuple(records))


def parse_safety_board_text(text: str, requested_date: str, observed_date: str,
                            expected_count: Optional[int] = None,
                            selected_filter: str = "all") -> RosterParseResult:
    """Parse the read-only ``今日晚寝打卡`` board, never the assist roster.

    The default ``all`` view is accepted only when its visible board is wholly
    made of ``未打卡`` records.  A mixed class board is accepted only when the
    caller has independently validated the official ``未打卡`` filter and
    passes ``selected_filter="pending"``.  The selected view must then contain
    exactly its pending rows, never completed or already-confirmed rows.
    """
    try:
        requested = date.fromisoformat(requested_date)
        observed = date.fromisoformat(observed_date)
    except (TypeError, ValueError):
        return RosterParseResult(False, str(requested_date), error="请求或观察日期无效")
    if requested.isoformat() != requested_date or observed.isoformat() != observed_date:
        return RosterParseResult(False, requested_date, error="请求或观察日期格式无效")
    if requested_date != observed_date:
        return RosterParseResult(False, requested_date, business_date=observed_date,
                                 error="页面日期与请求日期不一致")
    if selected_filter not in {"all", "pending"}:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 error="看板筛选状态未核验")
    if not isinstance(text, str) or not text.strip():
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 error="页面正文为空")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not SAFETY_TITLE_RE.fullmatch(lines[0]):
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 error="页面来源不是今日晚寝打卡看板")
    if any("打卡催办协助" in line or COUNT_RE.search(line) for line in lines):
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 error="页面来源不是今日晚寝打卡看板")

    index = 1
    if index < len(lines) and lines[index] == "查看历史":
        index += 1
    summary: dict[str, int] = {}
    for label in ("应打卡", "已打卡", "已代确认", "未打卡"):
        if index >= len(lines) or lines[index] != label or index + 1 >= len(lines):
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     error="看板汇总结构不完整")
        value = lines[index + 1]
        if not SAFETY_COUNT_RE.fullmatch(value):
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     error="看板汇总人数无效")
        summary[label] = int(value)
        index += 2
    if index >= len(lines) or not SAFETY_NOTICE_RE.fullmatch(lines[index]):
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 error="看板安全代确认说明缺失")
    index += 1

    filters: dict[str, int] = {}
    for label in ("全部", "未打卡", "已打卡", "已代确认"):
        if index >= len(lines):
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     error="看板筛选计数不完整")
        match = SAFETY_FILTER_RE.fullmatch(lines[index])
        if not match or match.group("label") != label:
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     error="看板筛选计数不完整")
        filters[label] = int(match.group("count"))
        index += 1

    pending = summary["未打卡"]
    total = filters["全部"]
    if (summary["应打卡"] != total or
            summary["已打卡"] + summary["已代确认"] + pending != summary["应打卡"] or
            filters["未打卡"] != pending or filters["已打卡"] != summary["已打卡"] or
            filters["已代确认"] != summary["已代确认"]):
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=pending,
                                 error="看板汇总与筛选计数不一致")
    if selected_filter == "all" and (summary["已打卡"] != 0 or
                                      summary["已代确认"] != 0 or pending != total):
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=pending,
                                 error="看板包含已打卡或已代确认记录，已暂停")
    declared = pending if selected_filter == "pending" else total
    if expected_count is not None and declared != expected_count:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="看板人数与预期不一致")

    # ``Loading`` is a transient UI footer, not a completeness marker.  The
    # declared count and exact row shape below are the completeness checks, so
    # a settled page without that footer remains valid.
    loading_index = next((pos for pos in range(index, len(lines)) if lines[pos] == "Loading"), len(lines))
    body_lines = lines[index:loading_index]
    if declared == 0:
        if body_lines and (len(body_lines) != 1 or body_lines[0] not in SAFETY_EMPTY_LINES):
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="空看板状态不完整")
        return RosterParseResult(True, requested_date, business_date=requested_date,
                                 declared_count=0, records=())

    if len(body_lines) != declared * 4:
        return RosterParseResult(False, requested_date, business_date=requested_date,
                                 declared_count=declared, error="看板记录未完整加载")
    records: list[RosterRecord] = []
    seen: set[str] = set()
    for index in range(0, len(body_lines), 4):
        name, status, action, id_line = body_lines[index:index + 4]
        if not NAME_RE.fullmatch(name) or status != "未打卡" or action != "安全代确认":
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="看板记录状态或操作不明确")
        id_match = ID_RE.fullmatch(id_line)
        if not id_match:
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="看板记录缺少有效学号")
        ident = id_match.group("id")
        if ident in seen:
            return RosterParseResult(False, requested_date, business_date=requested_date,
                                     declared_count=declared, error="看板包含重复学号")
        seen.add(ident)
        records.append(RosterRecord(ident, name, status))
    return RosterParseResult(True, requested_date, business_date=requested_date,
                             declared_count=declared, records=tuple(records))

