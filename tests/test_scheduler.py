"""Scheduler 单元测试 + 已知 bug 的回归测试。"""

import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from zhipu_planctl.scheduler import Scheduler, _DEFAULT_RETRY_MAX as _RETRY_MAX


class WindowExpiredTest(unittest.TestCase):
    """回归 C1：_is_window_expired 必须能正确比较带 tz 的 ISO 时间字符串。"""

    def test_future_returns_false(self):
        s = Scheduler(cold_start_times=["07:00"])
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.assertFalse(s._is_window_expired(future))

    def test_past_returns_true(self):
        s = Scheduler(cold_start_times=["07:00"])
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(s._is_window_expired(past))

    def test_invalid_returns_true(self):
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

    def test_invalid_numeric_range(self):
        for bad in ("24:00", "23:60", "-1:00", "10:-1", "aa:00"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Scheduler._parse_time(bad)


# ────────────── 冷启动验证 + 重试测试（含回归 C2）──────────────


def _quota_ns(*, ok=True, expired=True, utilization=0.0):
    """构造一个兼容 check_quota_now 解析的 SimpleNamespace 返回值。"""
    if expired:
        reset_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    else:
        reset_iso = (datetime.now(timezone.utc) + timedelta(hours=4, minutes=59)).isoformat()
    tier = SimpleNamespace(
        name="five_hour", utilization=utilization, resets_at=reset_iso,
    )
    return SimpleNamespace(
        ok=ok, level="pro", tiers=[tier],
        error=None if ok else "mock error",
        queried_at=0, credential_valid=ok,
    )


def _make_client(quota_side_effect, cold_start_side_effect):
    """构造一个带 side_effect 的 MagicMock client。"""
    c = MagicMock()
    c.query_quota.side_effect = quota_side_effect
    c.cold_start.side_effect = cold_start_side_effect
    return c


class ColdStartTest(unittest.TestCase):
    """回归 C2：cold_start_if_needed 验证 + 重试逻辑。"""

    @patch("zhipu_planctl.scheduler.time.sleep")
    def test_force_verifies_window_active(self, _sleep):
        """force=True: 冷启动成功后 quota 显示窗口活跃才算成功。"""
        s = Scheduler(cold_start_times=["07:00"])
        client = _make_client(
            quota_side_effect=[
                _quota_ns(expired=True),   # 初始检查
                _quota_ns(expired=False),  # 冷启动后验证
            ],
            cold_start_side_effect=[True],
        )
        result = s.cold_start_if_needed(client, "glm-4-air", force=True)
        self.assertTrue(result["cold_started"])
        self.assertIn("成功", result["reason"])
        self.assertEqual(client.cold_start.call_count, 1)
        self.assertEqual(client.query_quota.call_count, 2)

    @patch("zhipu_planctl.scheduler.time.sleep")
    def test_force_retries_on_cold_start_failure(self, _sleep):
        """force=True: cold_start 返回 False 时重试直到成功。"""
        s = Scheduler(cold_start_times=["07:00"])
        client = _make_client(
            quota_side_effect=[
                _quota_ns(expired=True),   # 初始
                _quota_ns(expired=False),  # 成功后的验证
            ],
            cold_start_side_effect=[False, False, True],
        )
        result = s.cold_start_if_needed(client, "glm-4-air", force=True)
        self.assertTrue(result["cold_started"])
        self.assertEqual(client.cold_start.call_count, 3)

    @patch("zhipu_planctl.scheduler.time.sleep")
    def test_force_retries_when_quota_still_expired(self, _sleep):
        """force=True: cold_start 返回 True 但 quota 仍显示过期，重试。"""
        s = Scheduler(cold_start_times=["07:00"])
        client = _make_client(
            quota_side_effect=[
                _quota_ns(expired=True),   # 初始
                _quota_ns(expired=True),   # 第 1 次验证：仍过期
                _quota_ns(expired=False),  # 第 2 次验证：活跃了
            ],
            cold_start_side_effect=[True, True],
        )
        result = s.cold_start_if_needed(client, "glm-4-air", force=True)
        self.assertTrue(result["cold_started"])
        self.assertEqual(client.cold_start.call_count, 2)

    @patch("zhipu_planctl.scheduler.time.sleep")
    def test_force_exhausts_retries(self, _sleep):
        """force=True: 所有重试都失败时返回 cold_started=False。"""
        s = Scheduler(cold_start_times=["07:00"])
        client = _make_client(
            quota_side_effect=[_quota_ns(expired=True)],  # 只在初始调用一次
            cold_start_side_effect=[False] * _RETRY_MAX,
        )
        result = s.cold_start_if_needed(client, "glm-4-air", force=True)
        self.assertFalse(result["cold_started"])
        self.assertIn("未确认", result["reason"])
        self.assertEqual(client.cold_start.call_count, _RETRY_MAX)

    @patch("zhipu_planctl.scheduler.time.sleep")
    def test_no_force_skips_if_window_active(self, _sleep):
        """force=False: 窗口未过期时直接跳过。"""
        s = Scheduler(cold_start_times=["07:00"])
        client = MagicMock()
        client.query_quota.return_value = _quota_ns(expired=False)

        result = s.cold_start_if_needed(client, "glm-4-air", force=False)
        self.assertFalse(result["cold_started"])
        self.assertIn("仍在有效期内", result["reason"])
        client.cold_start.assert_not_called()

    @patch("zhipu_planctl.scheduler.time.sleep")
    def test_no_force_one_shot_no_retry(self, _sleep):
        """force=False: 一次尝试不重试。"""
        s = Scheduler(cold_start_times=["07:00"])
        client = _make_client(
            quota_side_effect=[
                _quota_ns(expired=True),   # 初始
                _quota_ns(expired=True),   # 验证 (仍过期)
            ],
            cold_start_side_effect=[True],
        )
        result = s.cold_start_if_needed(client, "glm-4-air", force=False)
        self.assertFalse(result["cold_started"])
        self.assertEqual(client.cold_start.call_count, 1)


# ─────────────────── tick 分支测试 ───────────────────


def _make_mock_client(*, ok=True, expired=True, cold_ok=True, num_quota_calls=1):
    """构造一个最小可用 client mock，返回可定制的 quota tier。"""
    if expired:
        reset_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    else:
        reset_iso = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()

    tier = SimpleNamespace(
        name="five_hour", utilization=50.0, resets_at=reset_iso,
    )
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
    """tick 的核心分支验证。"""

    def test_quota_only_when_interval_elapsed(self):
        s = Scheduler(cold_start_times=["07:00"], quota_check_interval_minutes=5)
        events = []

        s.tick(_make_mock_client(), "glm-4-air", "hi",
               on_cold_start=lambda r: events.append("cs"),
               on_quota=lambda q: events.append("quota"))
        self.assertEqual(events, ["quota"])

    def test_idle_when_quota_recent_and_not_in_slot(self):
        s = Scheduler(cold_start_times=["07:00"], quota_check_interval_minutes=5)
        s._last_quota_check = datetime.now()
        events = []

        s.tick(_make_mock_client(), "glm-4-air", "hi",
               on_cold_start=lambda r: events.append("cs"),
               on_quota=lambda q: events.append("quota"))
        self.assertEqual(events, [])

    def test_dedupes_cold_start_within_same_slot(self):
        now = datetime.now()
        s = Scheduler(cold_start_times=[f"{now.hour:02d}:{now.minute:02d}"])
        s._processed_slots.add((now.hour, now.minute))
        events = []

        s.tick(_make_mock_client(expired=True), "glm-4-air", "hi",
               on_cold_start=lambda r: events.append("cs"),
               on_quota=lambda q: events.append("quota"))
        self.assertNotIn("cs", events)

    @patch("zhipu_planctl.scheduler.time.sleep")
    def test_cold_start_failure_does_not_mark_slot(self, _sleep):
        """冷启动失败时 slot 不标记，下次 tick 仍可重试。"""
        now = datetime.now()
        s = Scheduler(cold_start_times=[f"{now.hour:02d}:{now.minute:02d}"])

        c = MagicMock()
        c.query_quota.return_value = _quota_ns(expired=True)
        c.cold_start.return_value = False

        events = []
        s.tick(c, "glm-4-air", "hi",
               on_cold_start=lambda r: events.append(r),
               on_quota=lambda q: None)

        slot = (now.hour, now.minute)
        self.assertNotIn(slot, s._processed_slots)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["cold_started"])


# ─────────────────── 静默时段测试 ───────────────────


class QuietHoursTest(unittest.TestCase):
    """00:00-05:59 北京时间为静默时段，不查额度。"""

    def _utc_at_beijing(self, hour: int, minute: int = 0) -> datetime:
        """构造一个 UTC 时间，使得北京时间 = hour:minute。"""
        return datetime(2026, 7, 28, hour - 8, minute, tzinfo=timezone.utc) if hour >= 8 \
            else datetime(2026, 7, 28, hour + 16, minute, tzinfo=timezone.utc)

    def _patch_dt(self, beijing_hour: int, beijing_minute: int = 0):
        dt = self._utc_at_beijing(beijing_hour, beijing_minute)
        return patch("zhipu_planctl.scheduler.datetime", wraps=datetime)

    def test_is_quiet_hours_true_0000(self):
        with patch("zhipu_planctl.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = self._utc_at_beijing(0, 0)
            mock_dt.strftime = datetime.strftime
            from zhipu_planctl.scheduler import Scheduler
            self.assertTrue(Scheduler._is_quiet_hours())

    def test_is_quiet_hours_true_0359(self):
        with patch("zhipu_planctl.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = self._utc_at_beijing(3, 59)
            mock_dt.strftime = datetime.strftime
            from zhipu_planctl.scheduler import Scheduler
            self.assertTrue(Scheduler._is_quiet_hours())

    def test_is_quiet_hours_true_0559(self):
        with patch("zhipu_planctl.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = self._utc_at_beijing(5, 59)
            mock_dt.strftime = datetime.strftime
            from zhipu_planctl.scheduler import Scheduler
            self.assertTrue(Scheduler._is_quiet_hours())

    def test_is_quiet_hours_false_0600(self):
        with patch("zhipu_planctl.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = self._utc_at_beijing(6, 0)
            mock_dt.strftime = datetime.strftime
            from zhipu_planctl.scheduler import Scheduler
            self.assertFalse(Scheduler._is_quiet_hours())

    def test_is_quiet_hours_false_1200(self):
        with patch("zhipu_planctl.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = self._utc_at_beijing(12, 0)
            mock_dt.strftime = datetime.strftime
            from zhipu_planctl.scheduler import Scheduler
            self.assertFalse(Scheduler._is_quiet_hours())

    def test_is_quiet_hours_false_2000(self):
        with patch("zhipu_planctl.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = self._utc_at_beijing(20, 0)
            mock_dt.strftime = datetime.strftime
            from zhipu_planctl.scheduler import Scheduler
            self.assertFalse(Scheduler._is_quiet_hours())

    def test_tick_skips_quota_during_quiet_hours_simple(self):
        """静默时段的纯 quota tick 不调用 client 也不更新 _last_quota_check。"""
        s = Scheduler(cold_start_times=["08:00"], quota_check_interval_minutes=5)
        s._last_quota_check = None
        client = _make_mock_client()
        events = []

        with patch.object(s, "_is_quiet_hours", return_value=True):
            s.tick(client, "glm-4-air", "hi",
                   on_cold_start=lambda r: events.append(("cs", r)),
                   on_quota=lambda q: events.append(("quota", q)))

        self.assertEqual(events, [])
        self.assertIsNone(s._last_quota_check)

    def test_tick_allows_cold_start_during_quiet_hours(self):
        """静默时段内，冷启动槽位仍然正常执行。"""
        now = datetime.now()
        s = Scheduler(cold_start_times=[f"{now.hour:02d}:{now.minute:02d}"])
        s._processed_slots = set()
        client = _make_client(
            quota_side_effect=[
                _quota_ns(expired=True),   # 初始检查
                _quota_ns(expired=False),  # 冷启动后验证
            ],
            cold_start_side_effect=[True],
        )
        events = []

        with patch.object(s, "_is_quiet_hours", return_value=True):
            with patch("zhipu_planctl.scheduler.time.sleep"):
                s.tick(client, "glm-4-air", "hi",
                       on_cold_start=lambda r: events.append(("cs", r)),
                       on_quota=lambda q: events.append(("quota", q)))

        self.assertEqual(len(events), 2)  # on_cold_start + on_quota (is_quota_time)
        self.assertEqual(events[0][0], "cs")
        self.assertTrue(events[0][1]["cold_started"])
        self.assertEqual(events[1][0], "quota")

    def test_tick_quota_resumes_after_quiet_hours(self):
        """出静默时段后正常查额度。"""
        s = Scheduler(cold_start_times=["08:00"], quota_check_interval_minutes=5)
        s._last_quota_check = None
        client = _make_mock_client()
        events = []

        with patch.object(s, "_is_quiet_hours", return_value=False):
            s.tick(client, "glm-4-air", "hi",
                   on_cold_start=lambda r: events.append(("cs", r)),
                   on_quota=lambda q: events.append(("quota", q)))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "quota")
        self.assertIsNotNone(s._last_quota_check)

    def test_tick_quota_updated_after_quiet_hours_resume(self):
        """出静默后 _last_quota_check 被正确更新，且 quota 不再重复触发。"""
        s = Scheduler(cold_start_times=["08:00"], quota_check_interval_minutes=5)
        s._last_quota_check = None
        client = _make_mock_client()
        events = []

        with patch.object(s, "_is_quiet_hours", return_value=False):
            s.tick(client, "glm-4-air", "hi",
                   on_cold_start=lambda r: events.append("cs"),
                   on_quota=lambda q: events.append("quota"))
        # 第一次 tick: 查了额度
        self.assertEqual(events, ["quota"])

        # 第二次 tick: 间隔未过 5min，不应再查
        events.clear()
        with patch.object(s, "_is_quiet_hours", return_value=False):
            s.tick(client, "glm-4-air", "hi",
                   on_cold_start=lambda r: events.append("cs"),
                   on_quota=lambda q: events.append("quota"))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
