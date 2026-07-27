import unittest

from zhipu_planctl.cli import CommandRouter, _DEFAULT_COLD_START_TIMES


class _Scheduler:
    def __init__(self):
        self.updated = None

    def check_quota_now(self, _client):
        return {"ok": True, "five_hour_utilization": 12.3, "five_hour_resets_at": None}

    def slots_as_strings(self):
        return _DEFAULT_COLD_START_TIMES

    def update_cold_start_times(self, times):
        self.updated = times


class _Feishu:
    def __init__(self):
        self.status_calls = []
        self.messages = []

    def send_status(self, quota, chat_id=None, cold_start_times=None):
        self.status_calls.append((quota, chat_id, cold_start_times))

    def send_message(self, text, chat_id=None):
        self.messages.append((text, chat_id))


class CommandRouterTest(unittest.TestCase):
    def test_default_windows_match_required_plan(self):
        self.assertEqual(_DEFAULT_COLD_START_TIMES, ["06:00", "11:00", "16:00", "21:00"])

    def test_status_command_with_leftover_mention_prefix(self):
        scheduler = _Scheduler()
        feishu = _Feishu()
        router = CommandRouter(scheduler, object(), feishu, "glm-4.7", "hi")

        router.dispatch("cli 查额度", "oc_123", "ou_456")

        self.assertEqual(len(feishu.status_calls), 1)
        self.assertEqual(feishu.status_calls[0][1], "oc_123")

    def test_set_times_with_leftover_mention_prefix(self):
        scheduler = _Scheduler()
        feishu = _Feishu()
        router = CommandRouter(scheduler, object(), feishu, "glm-4.7", "hi")

        router.dispatch("cli 冷启动时间 06:00 11:00 16:00 21:00", "oc_123", "ou_456")

        self.assertEqual(scheduler.updated, ["06:00", "11:00", "16:00", "21:00"])


if __name__ == "__main__":
    unittest.main()
