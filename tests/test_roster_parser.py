import unittest

from roster_parser import parse_roster_text


class RosterParserTests(unittest.TestCase):
    def test_parses_observed_date_and_four_line_rows(self):
        text = """打卡催办协助
本班未打卡同学（2）
查看历史
张三
未打卡
回填
学号 20230001
李四
未打卡
回填
学号 20230002
本次已回填 0 / 2。名单随打卡情况实时更新，同学完成打卡后自动从本列表移除； 你回填的情况会同步给辅导员参考。
Loading
"""
        result = parse_roster_text(text, "2026-10-15", expected_count=2, observed_date="2026-10-15")
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.records[0].id, "20230001")
        self.assertEqual(result.audit_summary, "名单解析成功；记录 2 条")

    def test_parses_empty_roster_state(self):
        text = """本班未打卡同学（0）
查看历史
本班同学都已完成打卡
本次已回填 0 / 0。
Loading
"""
        result = parse_roster_text(text, "2026-10-15", expected_count=0,
                                   observed_date="2026-10-15")
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 0)

    def test_rejects_truncated_or_unrecognized_empty_state(self):
        truncated = """本班未打卡同学（1）
张三
未打卡
回填
学号 20230001
本次已回填 0 / 1。名单随打卡情况实时更新，Loading
"""
        result = parse_roster_text(truncated, "2026-10-15", expected_count=1,
                                   observed_date="2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("结束摘要", result.error)

        empty = """本班未打卡同学（0）
本次已回填 0 / 0。
"""
        result = parse_roster_text(empty, "2026-10-15", expected_count=0,
                                   observed_date="2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("空名单", result.error)

    def test_rejects_missing_rows_or_changed_date(self):
        text = "本班未打卡同学（30）\n未打卡 回填 学号"
        result = parse_roster_text(text, "2026-10-15", expected_count=30, observed_date="2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("结束摘要", result.error)
        changed = parse_roster_text(text, "2026-10-14", expected_count=30, observed_date="2026-10-15")
        self.assertFalse(changed.ok)
        self.assertIn("日期", changed.error)

    def test_rejects_duplicate_and_count_mismatch(self):
        text = """本班未打卡同学（2）
张三
未打卡
回填
学号 20230001
李四
未打卡
回填
学号 20230001
本次已回填 0 / 2
"""
        result = parse_roster_text(text, "2026-10-15", expected_count=2, observed_date="2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("重复", result.error)

    def test_rejects_conflicting_embedded_dates(self):
        text = """名单日期：2026-10-15
打卡日期：2026-10-14
本班未打卡同学（0）
本班同学都已完成打卡
本次已回填 0 / 0。
"""
        result = parse_roster_text(text, "2026-10-15", expected_count=0,
                                   observed_date="2026-10-15")
        self.assertFalse(result.ok)
        self.assertIn("多个不一致日期", result.error)

    def test_never_puts_personal_data_in_audit_summary(self):
        text = """本班未打卡同学（1）
张三
未打卡
回填
学号 20230001
本次已回填 0 / 1
"""
        result = parse_roster_text(text, "2026-10-15", expected_count=1, observed_date="2026-10-15")
        self.assertNotIn("张三", result.audit_summary)
        self.assertNotIn("20230001", result.audit_summary)


if __name__ == "__main__":
    unittest.main()
