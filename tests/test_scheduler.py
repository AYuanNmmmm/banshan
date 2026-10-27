"""定时调度测试：5 字段 cron 的匹配与人类可读描述。"""
from __future__ import annotations

import time
import unittest

from panmigrate.scheduler import cron_matches, describe_cron


def at(y, mo, d, h, mi, wday):
    """构造一个指定时刻的 struct_time（t_wday: 周一=0 ... 周日=6）。"""
    return time.struct_time((y, mo, d, h, mi, 0, wday, 1, 0))


SATURDAY_0300 = at(2026, 10, 24, 3, 0, 5)


class CronMatchesTest(unittest.TestCase):
    def test_exact_time(self):
        self.assertTrue(cron_matches("0 3 * * *", SATURDAY_0300))
        self.assertFalse(cron_matches("1 3 * * *", SATURDAY_0300))
        self.assertFalse(cron_matches("0 4 * * *", SATURDAY_0300))

    def test_star_matches_everything(self):
        self.assertTrue(cron_matches("* * * * *", SATURDAY_0300))

    def test_step_values(self):
        self.assertTrue(cron_matches("*/15 * * * *", at(2026, 10, 24, 3, 30, 5)))
        self.assertFalse(cron_matches("*/15 * * * *", at(2026, 10, 24, 3, 31, 5)))
        self.assertTrue(cron_matches("0 */2 * * *", at(2026, 10, 24, 4, 0, 5)))
        self.assertFalse(cron_matches("0 */2 * * *", at(2026, 10, 24, 3, 0, 5)))

    def test_ranges(self):
        self.assertTrue(cron_matches("0-30 * * * *", at(2026, 10, 24, 3, 15, 5)))
        self.assertFalse(cron_matches("0-30 * * * *", at(2026, 10, 24, 3, 45, 5)))

    def test_lists(self):
        self.assertTrue(cron_matches("0 1,3,5 * * *", SATURDAY_0300))
        self.assertFalse(cron_matches("0 1,5,7 * * *", SATURDAY_0300))

    def test_weekday_uses_sunday_zero(self):
        # 2026-10-24 是周六，cron 里周六 = 6
        self.assertTrue(cron_matches("0 3 * * 6", SATURDAY_0300))
        self.assertFalse(cron_matches("0 3 * * 0", SATURDAY_0300))
        # 周日既可以用 0 也可以用 7
        sunday = at(2026, 10, 25, 3, 0, 6)
        self.assertTrue(cron_matches("0 3 * * 0", sunday))
        self.assertTrue(cron_matches("0 3 * * 7", sunday))

    def test_day_of_month_and_month(self):
        new_year = at(2026, 1, 1, 0, 0, 3)
        self.assertTrue(cron_matches("0 0 1 1 *", new_year))
        self.assertFalse(cron_matches("0 0 2 1 *", new_year))

    def test_malformed_expression_never_matches(self):
        for expr in ("", "0 3 * *", "0 3 * * * *", "not a cron"):
            self.assertFalse(cron_matches(expr, SATURDAY_0300), expr)


class DescribeCronTest(unittest.TestCase):
    def test_daily(self):
        self.assertEqual(describe_cron("0 3 * * *"), "每天 03:00")

    def test_hourly(self):
        self.assertEqual(describe_cron("5 * * * *"), "每小时第 5 分钟")

    def test_every_n_minutes(self):
        self.assertEqual(describe_cron("*/10 * * * *"), "每 10 分钟")

    def test_fallback_keeps_expression(self):
        self.assertIn("0 3 * * 1", describe_cron("0 3 * * 1"))

    def test_bad_expression_is_explained(self):
        self.assertIn("5 个字段", describe_cron("0 3 *"))


if __name__ == "__main__":
    unittest.main()
