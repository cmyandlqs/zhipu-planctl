"""Scheduler 单元测试 + 已知 bug 的回归测试。"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from zhipu_planctl.scheduler import Scheduler


class WindowExpiredTest(unittest.TestCase):
    """回归 C1：_is_window_expired 必须能正确比较带 tz 的 ISO 时间字符串。"""

    def test_future_returns_false(self):
        """未来时间 → 窗口未过期。"""
        s = Scheduler(cold_start_times=["07:00"])
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.assertFalse(s._is_window_expired(future))

    def test_past_returns_true(self):
        """过去时间 → 窗口已过期。"""
        s = Scheduler(cold_start_times=["07:00"])
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(s._is_window_expired(past))

    def test_invalid_returns_true(self):
        """None / 空字符串 / 非法日期 → 全部按"已过期"处理（保守触发冷启动）。"""
        s = Scheduler(cold_start_times=["07:00"])
        self.assertTrue(s._is_window_expired(None))
        self.assertTrue(s._is_window_expired(""))
        self.assertTrue(s._is_window_expired("not-a-date"))


class ParseTimeTest(unittest.TestCase):
    """O1：_parse_time 必须严格校验 "HH:MM" 格式，错误直接抛。"""

    def test_valid(self):
        self.assertEqual(Scheduler._parse_time("07:30"), (7, 30))
        self.assertEqual(Scheduler._parse_time("00:00"), (0, 0))
        self.assertEqual(Scheduler._parse_time("23:59"), (23, 59))

    def test_invalid_segment_count(self):
        for bad in ("7", "7:30:00", "", "07:"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Scheduler._parse_time(bad)


def _make_mock_client(*, ok=True, expired=True, cold_ok=True):
    """构造一个最小可用的 zhipu_client mock。返回的对象有 .query_quota() 和 .cold_start()。"""
    if expired:
        reset_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    else:
        reset_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    tier = SimpleNamespace(name="five_hour", utilization=50.0, resets_at=reset_iso)
    quota_result = SimpleNamespace(
        ok=ok, level="pro", tiers=[tier],
        error=None if ok else "mock error",
        queried_at=0, credential_valid=ok,
    )

    class _Client:
        def query_quota(_self):
            return quota_result

        def cold_start(_self, model="glm-4-air", prompt="hi"):
            return cold_ok

    return _Client()


class TickTest(unittest.TestCase):
    """tick 的核心分支验证（不需要真 API，用 mock 代替 zhipu_client）。"""

    def test_quota_only_when_interval_elapsed(self):
        """非冷启动时间槽、首次 tick：必然查一次额度（_last_quota_check 为 None）。"""
        s = Scheduler(cold_start_times=["07:00"], quota_check_interval_minutes=5)
        events = []

        s.tick(_make_mock_client(), "glm-4-air", "hi",
               on_cold_start=lambda r: events.append("cs"),
               on_quota=lambda q: events.append("quota"))
        self.assertEqual(events, ["quota"])

    def test_idle_when_quota_recent_and_not_in_slot(self):
        """非冷启动时间槽 + 刚刚查过 → 本 tick 不做任何事。"""
        s = Scheduler(cold_start_times=["07:00"], quota_check_interval_minutes=5)
        s._last_quota_check = datetime.now()  # 假装刚刚查过
        events = []

        s.tick(_make_mock_client(), "glm-4-air", "hi",
               on_cold_start=lambda r: events.append("cs"),
               on_quota=lambda q: events.append("quota"))
        self.assertEqual(events, [])

    def test_dedupes_cold_start_within_same_slot(self):
        """_processed_slots 已经包含当前 slot 时，不会再次触发冷启动。"""
        now = datetime.now()
        s = Scheduler(cold_start_times=[f"{now.hour:02d}:{now.minute:02d}"])
        s._processed_slots.add((now.hour, now.minute))   # 假装已经处理过
        events = []

        s.tick(_make_mock_client(expired=True), "glm-4-air", "hi",
               on_cold_start=lambda r: events.append("cs"),
               on_quota=lambda q: events.append("quota"))
        self.assertNotIn("cs", events)


if __name__ == "__main__":
    unittest.main()
