import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from official_adapter import (AdapterError, BOARD_ROUTE, BoardSnapshot, TrialConfig,
                              TrialResult, _read_board_from_ui, execute_batch, execute_trial)


TODAY = "2026-10-15"
RECORD = {"id": "12345678", "name": "测试甲", "status": "未打卡"}


class FakeLocator:
    def __init__(self, *, count=1, text="", click_error=None, on_click=None, tag="SPAN"):
        self._count = count
        self._text = text
        self._click_error = click_error
        self._on_click = on_click
        self._tag = tag
        self.clicks = 0
        self.fills = []

    def count(self):
        return self._count

    def nth(self, _index):
        return self

    def filter(self, **_kwargs):
        return self

    def get_by_text(self, _text, exact=False):
        return self

    def click(self):
        self.clicks += 1
        if self._click_error:
            raise self._click_error
        if self._on_click:
            self._on_click()

    def evaluate(self, _expression):
        return self._tag

    def fill(self, value):
        self.fills.append(value)

    def inner_text(self, timeout=None):
        return self._text


class FakeCardLocator(FakeLocator):
    def __init__(self, action):
        super().__init__()
        self._action = action

    def get_by_text(self, _text, exact=False):
        return self._action


BOARD_BODY = "安全代确认\n学号 12345678"
FORM_BODY = "测试甲 · 学号 12345678\n测试班级\n核实方式\n电话\n当面\n室友转达\n提交代确认"


class TransitionBody:
    """Models the official SPA page transition after a card click.

    The hash URL and document title change before the lazily loaded confirm
    chunk mounts, and the board page is hidden during the transition, so the
    first reads after the click observe an empty body before the form
    renders.  ``ready_after`` counts the empty reads once the form opened.
    """

    def __init__(self, page, ready_after):
        self._page = page
        self._ready_after = ready_after
        self.reads = 0

    def inner_text(self, timeout=None):
        self.reads += 1
        if not self._page.form_open:
            return BOARD_BODY
        if self.reads <= self._ready_after:
            return ""
        return FORM_BODY


class FakePage:
    url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#" + BOARD_ROUTE

    def __init__(self, submit_error=None, form_ready_reads=0):
        self.form_open = False
        self.body = TransitionBody(self, form_ready_reads)
        self.action = FakeLocator(on_click=self.open_form)
        self.cards = FakeCardLocator(self.action)
        self.method = FakeLocator(tag="SPAN")
        self.textarea = FakeLocator()
        self.submit = FakeLocator(click_error=submit_error, on_click=self.submit_action)
        self.navigations = 0
        self.timeouts = 0

    def title(self):
        return "安全代确认" if self.url.endswith("/confirm") else "班级打卡看板"

    def open_form(self):
        self.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/confirm"
        self.form_open = True

    def submit_action(self):
        self.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board"

    def wait_for_timeout(self, _milliseconds):
        self.timeouts += 1

    def locator(self, selector):
        return {
            "body": self.body,
            ".padk-person": self.cards,
            ".radio-item": self.method,
            "textarea.uni-textarea-textarea": self.textarea,
            ".padk-bar-btn--primary": self.submit,
        }[selector]

    def get_by_text(self, _text, exact=False):
        return self.cards

    def goto(self, _url, **_kwargs):
        self.navigations += 1
        self.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#" + BOARD_ROUTE

    def wait_for_url(self, _pattern, timeout=None):
        if not self.url.endswith("/board"):
            raise TimeoutError("not on board")


def snapshot(status="未打卡", pending=1):
    return BoardSnapshot(TODAY, BOARD_ROUTE, "班级打卡看板",
                         ({"id": RECORD["id"], "name": RECORD["name"], "status": status},), pending,
                         "pending" if status == "未打卡" else "all")


class OfficialAdapterTests(unittest.TestCase):
    def config(self, page, snapshots):
        queue = list(snapshots)

        def read_board(_page):
            return queue.pop(0)

        return TrialConfig(
            page=page,
            supervised_approved=True,
            read_board=read_board,
            now=lambda: datetime.fromisoformat(TODAY + "T08:00:00+08:00"),
        )

    def test_supervision_gate_blocks_without_touching_page(self):
        page = Mock()
        config = TrialConfig(page=page)
        with self.assertRaisesRegex(RuntimeError, "监督授权"):
            execute_trial(config, RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        page.assert_not_called()

    def test_success_requires_post_submit_confirmed_state_and_one_less_pending(self):
        page = FakePage()
        result = execute_trial(self.config(page, [snapshot(), snapshot("已代确认", 0)]),
                               RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "success")
        self.assertTrue(result.submitted)
        self.assertEqual(page.submit.clicks, 1)
        # The adapter waits for the official submit redirect; it does not
        # force-navigate or replay the form after the click.
        self.assertEqual(page.navigations, 0)

    def test_changed_post_state_is_partial_and_never_replayed(self):
        page = FakePage()
        result = execute_trial(self.config(page, [snapshot(), snapshot("已代确认", 1)]),
                               RECORD, "当面", "已当面核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "partial")
        self.assertEqual(page.submit.clicks, 1)

    def test_submit_transport_failure_is_unknown_without_retry(self):
        page = FakePage(submit_error=TimeoutError("ambiguous"))
        result = execute_trial(self.config(page, [snapshot()]),
                               RECORD, "室友转达", "已由室友转达并核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.submitted)
        self.assertEqual(page.submit.clicks, 1)

    def test_form_transition_waits_for_rendered_student_header(self):
        """The confirm page is a lazily loaded SPA route: the URL and document
        title change before the chunk mounts and the board is hidden during
        the transition, so the first body reads observe neither page.  The
        adapter must wait for the rendered form instead of stopping."""
        page = FakePage(form_ready_reads=2)
        result = execute_trial(self.config(page, [snapshot(), snapshot("已代确认", 0)]),
                               RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "success")
        self.assertEqual(page.submit.clicks, 1)
        self.assertGreaterEqual(page.body.reads, 3)

    def test_form_never_rendering_stops_without_submit(self):
        page = FakePage(form_ready_reads=10 ** 6)
        config = self.config(page, [snapshot()])
        config.navigation_timeout_ms = 300
        result = execute_trial(config, RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "unknown")
        self.assertIn("未显示目标学号", result.detail)
        self.assertEqual(page.submit.clicks, 0)

    def test_route_never_reaching_confirm_stops_without_submit(self):
        page = FakePage()
        page.action._on_click = None  # the card action never navigates
        config = self.config(page, [snapshot()])
        config.navigation_timeout_ms = 300
        result = execute_trial(config, RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "unknown")
        self.assertIn("未进入", result.detail)
        self.assertEqual(page.submit.clicks, 0)

    def test_method_option_must_be_unique_before_click(self):
        page = FakePage()
        page.method._count = 2  # two rows would match the label
        config = self.config(page, [snapshot()])
        result = execute_trial(config, RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "unknown")
        self.assertIn("核实方式选项不唯一", result.detail)
        self.assertEqual(page.submit.clicks, 0)

    def test_complete_board_includes_unchanged_students_who_already_checked_in(self):
        extra = {"id": "87654321", "name": "测试乙", "status": "已打卡"}
        before = BoardSnapshot(TODAY, BOARD_ROUTE, "班级打卡看板",
                               (RECORD, extra), 1, "all")
        confirmed = {**RECORD, "status": "已代确认"}
        after = BoardSnapshot(TODAY, BOARD_ROUTE, "班级打卡看板",
                              (confirmed, extra), 0, "all")
        page = FakePage()
        result = execute_trial(self.config(page, [before, after]), RECORD,
                               "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "success")
        self.assertEqual(page.submit.clicks, 1)

    def test_pending_view_reads_full_baseline_before_submit(self):
        extra = {"id": "87654321", "name": "测试乙", "status": "已打卡"}
        full_before = BoardSnapshot(TODAY, BOARD_ROUTE, "班级打卡看板",
                                    (RECORD, extra), 1, "all")
        full_after = BoardSnapshot(TODAY, BOARD_ROUTE, "班级打卡看板",
                                   ({**RECORD, "status": "已代确认"}, extra), 0, "all")
        page = FakePage()
        config = self.config(page, [])
        config.read_board = None
        with patch("official_adapter._read_board", side_effect=[
                snapshot(), full_before, snapshot(), full_after]) as reader, \
                patch("official_adapter._select_all_board_filter") as select_all, \
                patch("official_adapter._select_board_filter") as select_filter:
            result = execute_trial(config, RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "success")
        self.assertEqual(reader.call_count, 4)
        self.assertEqual(select_all.call_count, 2)
        select_filter.assert_called_once_with(page, "pending")

    def test_change_to_other_class_record_stops_after_submit(self):
        extra = {"id": "87654321", "name": "测试乙", "status": "已打卡"}
        before = BoardSnapshot(TODAY, BOARD_ROUTE, "班级打卡看板", (RECORD, extra), 1, "all")
        after = BoardSnapshot(TODAY, BOARD_ROUTE, "班级打卡看板",
                              ({**RECORD, "status": "已代确认"}, {**extra, "name": "变更姓名"}),
                              0, "all")
        page = FakePage()
        result = execute_trial(self.config(page, [before, after]), RECORD,
                               "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(result.status, "partial")
        self.assertEqual(page.submit.clicks, 1)

    def batch_inputs(self):
        roster = [{"id": str(12345678 + index), "name": f"测试{index}", "status": "未打卡"}
                  for index in range(3)]
        plans = [{"id": row["id"], "method": "电话", "explanation": "已电话核实安全"}
                 for row in roster]
        return roster, plans

    def test_batch_submits_sequentially_and_reports_only_verified_progress(self):
        roster, plans = self.batch_inputs()
        progress = []
        page = Mock()
        config = self.config(page, [])
        with patch("official_adapter.execute_trial", side_effect=[
                TrialResult("success", "已核验", True, row["id"]) for row in roster]) as trial:
            result = execute_batch(config, plans, roster, TODAY, on_progress=progress.append)
        self.assertEqual(result.as_dict(), {"status": "success",
                         "detail": "批次各条记录均已通过官方看板结果校验",
                         "submitted_count": 3, "total_count": 3})
        self.assertEqual(progress, [1, 2, 3])
        self.assertEqual([call.args[4] for call in trial.call_args_list],
                         [roster, roster[1:], roster[2:]])
        self.assertEqual([call.args[1]["id"] for call in trial.call_args_list],
                         [row["id"] for row in roster])

    def test_batch_stops_on_second_ambiguous_result_without_third_submit(self):
        for status in ("unknown", "partial"):
            with self.subTest(status=status):
                roster, plans = self.batch_inputs()
                progress = []
                with patch("official_adapter.execute_trial", side_effect=[
                        TrialResult("success", "已核验", True, roster[0]["id"]),
                        TrialResult(status, "结果不明", True, roster[1]["id"])]) as trial:
                    result = execute_batch(self.config(Mock(), []), plans, roster, TODAY,
                                           on_progress=progress.append)
                self.assertEqual(result.status, status)
                self.assertEqual(result.submitted_count, 1)
                self.assertEqual(result.total_count, 3)
                self.assertEqual(trial.call_count, 2)
                self.assertEqual(progress, [1])

    def test_batch_validates_all_plans_before_touching_page(self):
        roster, plans = self.batch_inputs()
        plans[2]["id"] = plans[0]["id"]
        page = Mock()
        with patch("official_adapter.execute_trial") as trial:
            with self.assertRaisesRegex(AdapterError, "重复"):
                execute_batch(self.config(page, []), plans, roster, TODAY)
        trial.assert_not_called()
        self.assertEqual(page.mock_calls, [])

    def test_batch_stops_when_progress_cannot_be_saved(self):
        roster, plans = self.batch_inputs()
        with patch("official_adapter.execute_trial", return_value=TrialResult("success", "已核验", True)) as trial:
            result = execute_batch(self.config(Mock(), []), plans, roster, TODAY,
                                   on_progress=Mock(side_effect=OSError("disk full")))
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.submitted_count, 1)
        self.assertEqual(trial.call_count, 1)

    def test_midnight_before_submit_stops_without_clicking_submit(self):
        page = FakePage()
        config = self.config(page, [snapshot()])
        clock_values = iter([datetime.fromisoformat(TODAY + "T23:59:59+08:00"),
                             datetime.fromisoformat("2026-10-16T00:00:00+08:00")])
        config.now = lambda: next(clock_values)
        with self.assertRaises(AdapterError):
            execute_trial(config, RECORD, "电话", "已电话核实安全", [RECORD], TODAY)
        self.assertEqual(page.submit.clicks, 0)

    def test_mixed_board_reads_status_words_in_cards_without_confusing_summary(self):
        notice = ("距今日打卡截止（22:30）还有1小时，点学生卡片可查看其打卡详情；"
                  "对确实无法本人打卡但已核实安全的同学，可发起「安全代确认」——"
                  "该记录不计为本人打卡，也不计缺勤。")
        header = ["测试班 · 今日晚寝打卡", "查看历史", "应打卡", "2", "已打卡", "1",
                  "已代确认", "0", "未打卡", "1", notice,
                  "全部（2）", "未打卡（1）", "已打卡（1）", "已代确认（0）"]
        body = header + ["测试甲", "未打卡", "安全代确认", "学号 12345678",
                         "测试乙", "已打卡", "查看详情", "学号 87654321"]
        card_fields = [
            {".padk-person-name": "测试甲", ".padk-chip": "未打卡", ".padk-person-sub": "学号 12345678"},
            {".padk-person-name": "测试乙", ".padk-chip": "已打卡", ".padk-person-sub": "学号 87654321"},
        ]
        cards = Mock()
        cards.count.return_value = 2
        card_mocks = []
        for fields in card_fields:
            card = Mock()
            card.locator.side_effect = lambda selector, fields=fields: FakeLocator(text=fields[selector])
            card_mocks.append(card)
        cards.nth.side_effect = lambda index: card_mocks[index]
        page = Mock()
        page.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#" + BOARD_ROUTE
        page.title.return_value = "班级打卡看板"
        page.locator.side_effect = lambda selector: {
            "body": FakeLocator(text="\n".join(body)),
            ".padk-tabs--board .padk-tab--on": FakeLocator(text="全部（2）"),
            ".padk-person": cards,
        }[selector]
        result = _read_board_from_ui(page, TODAY, "all")
        self.assertEqual(result.pending_count, 1)
        self.assertEqual([row["status"] for row in result.records], ["未打卡", "已打卡"])
        # The summary must also agree with each DOM chip; an intact header
        # cannot conceal a changed or misread card state.
        card_fields[1][".padk-chip"] = "未打卡"
        with self.assertRaisesRegex(AdapterError, "状态与汇总"):
            _read_board_from_ui(page, TODAY, "all")


class MixedBoardCardReaderTests(unittest.TestCase):
    """The mixed ``全部`` board is read through the card DOM because the strict
    parser only accepts an all-pending board."""

    def build_page(self, card_fields, body):
        cards = Mock()
        cards.count.return_value = len(card_fields)
        card_mocks = []
        for fields in card_fields:
            card = Mock()
            card.locator.side_effect = lambda selector, fields=fields: FakeLocator(text=fields[selector])
            card_mocks.append(card)
        cards.nth.side_effect = lambda index: card_mocks[index]
        page = Mock()
        page.url = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#" + BOARD_ROUTE
        page.title.return_value = "班级打卡看板"
        page.locator.side_effect = lambda selector: {
            "body": FakeLocator(text="\n".join(body)),
            ".padk-tabs--board .padk-tab--on": FakeLocator(text="全部（2）"),
            ".padk-person": cards,
        }[selector]
        return page

    def board(self):
        notice = ("距今日打卡截止（22:30）还有1小时，点学生卡片可查看其打卡详情；"
                  "对确实无法本人打卡但已核实安全的同学，可发起「安全代确认」——"
                  "该记录不计为本人打卡，也不计缺勤。")
        header = ["测试班 · 今日晚寝打卡", "查看历史", "应打卡", "2", "已打卡", "1",
                  "已代确认", "0", "未打卡", "1", notice,
                  "全部（2）", "未打卡（1）", "已打卡（1）", "已代确认（0）"]
        body = header + ["测试甲", "未打卡", "安全代确认", "学号 12345678",
                         "测试乙", "已打卡", "查看详情", "学号 87654321 · 20:09 已打卡"]
        return body

    def test_checked_in_card_time_suffix_is_tolerated(self):
        page = self.build_page([
            {".padk-person-name": "测试甲", ".padk-chip": "未打卡", ".padk-person-sub": "学号 12345678"},
            {".padk-person-name": "测试乙", ".padk-chip": "已打卡", ".padk-person-sub": "学号 87654321 · 20:09 已打卡"},
        ], self.board())
        result = _read_board_from_ui(page, TODAY, "all")
        self.assertEqual(result.pending_count, 1)
        self.assertEqual([row["id"] for row in result.records], ["12345678", "87654321"])
        self.assertEqual([row["status"] for row in result.records], ["未打卡", "已打卡"])

    def test_malformed_card_sub_is_still_rejected(self):
        page = self.build_page([
            {".padk-person-name": "测试甲", ".padk-chip": "未打卡", ".padk-person-sub": "学号 12345678"},
            {".padk-person-name": "测试乙", ".padk-chip": "已打卡", ".padk-person-sub": "详情 87654321"},
        ], self.board())
        with self.assertRaisesRegex(AdapterError, "姓名或学号无法核验"):
            _read_board_from_ui(page, TODAY, "all")


if __name__ == "__main__":
    unittest.main()
