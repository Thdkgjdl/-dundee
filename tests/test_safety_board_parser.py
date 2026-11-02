import unittest

from roster_parser import parse_safety_board_text


NOTICE = "距今日打卡截止（）还有 一段时间。点学生卡片可查看其打卡详情；对确实无法本人打卡但已核实安全的同学，可发起「安全代确认」——该记录不计为本人打卡，也不计缺勤。"


def board(rows, counts=(2, 0, 0, 2), filters=(2, 2, 0, 0)):
    lines = ["测试班级 · 今日晚寝打卡", "查看历史",
             "应打卡", str(counts[0]), "已打卡", str(counts[1]),
             "已代确认", str(counts[2]), "未打卡", str(counts[3]), NOTICE,
             f"全部（{filters[0]}）", f"未打卡（{filters[1]}）",
             f"已打卡（{filters[2]}）", f"已代确认（{filters[3]}）"]
    for name, ident in rows:
        lines.extend([name, "未打卡", "安全代确认", f"学号 {ident}"])
    lines.append("Loading")
    return "\n".join(lines)


class SafetyBoardParserTests(unittest.TestCase):
    def test_parses_all_pending_board(self):
        result = parse_safety_board_text(
            board([("测试甲", "0000000001"), ("测试乙", "0000000002")]),
            "2026-10-15", "2026-10-15", expected_count=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.records[0].status, "未打卡")

    def test_parses_zero_pending_board(self):
        text = board([], counts=(0, 0, 0, 0), filters=(0, 0, 0, 0)).replace(
            "Loading", "本班同学都已完成打卡\nLoading")
        result = parse_safety_board_text(text, "2026-10-15", "2026-10-15", expected_count=0)
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 0)

    def test_parses_empty_pending_filter_on_completed_board(self):
        text = board([], counts=(2, 2, 0, 0), filters=(2, 0, 2, 0))
        result = parse_safety_board_text(text, "2026-10-15", "2026-10-15",
                                         expected_count=0, selected_filter="pending")
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 0)

    def test_rejects_mixed_board_and_wrong_source(self):
        mixed = board([("测试甲", "0000000001")], counts=(2, 1, 0, 1), filters=(2, 1, 1, 0))
        result = parse_safety_board_text(mixed, "2026-10-15", "2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("已打卡", result.error)
        result = parse_safety_board_text(mixed, "2026-10-15", "2026-10-15",
                                         expected_count=1, selected_filter="pending")
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 1)
        assist = "本班未打卡同学（1）\n测试甲\n未打卡\n回填\n学号 0000000001\n本次已回填 0 / 1。"
        result = parse_safety_board_text(assist, "2026-10-15", "2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("来源", result.error)

    def test_rejects_unverified_filter_state(self):
        text = board([("测试甲", "0000000001")], counts=(2, 1, 0, 1), filters=(2, 1, 1, 0))
        result = parse_safety_board_text(text, "2026-10-15", "2026-10-15",
                                         selected_filter="completed")
        self.assertFalse(result.ok)
        self.assertIn("筛选", result.error)

    def test_rejects_incomplete_or_wrong_date(self):
        text = board([("测试甲", "0000000001")], counts=(1, 0, 0, 1),
                     filters=(1, 1, 0, 0)).replace("Loading", "")
        result = parse_safety_board_text(text, "2026-10-15", "2026-10-15")
        self.assertTrue(result.ok)
        truncated = board([("测试甲", "0000000001")], counts=(1, 0, 0, 1),
                          filters=(1, 1, 0, 0)).replace(
            "测试甲\n未打卡\n安全代确认\n学号 0000000001\nLoading", "测试甲\n未打卡\nLoading")
        result = parse_safety_board_text(truncated, "2026-10-15", "2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("记录未完整", result.error)
        result = parse_safety_board_text(board([("测试甲", "0000000001")]),
                                         "2026-10-15", "2026-10-14")
        self.assertFalse(result.ok)
        self.assertIn("日期", result.error)


if __name__ == "__main__":
    unittest.main()
