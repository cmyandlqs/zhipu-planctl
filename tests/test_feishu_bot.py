import json
import unittest

from zhipu_planctl.feishu_bot import FeishuBot


class FeishuBotEventTest(unittest.TestCase):
    def test_leading_mention_keeps_command_text(self):
        bot = FeishuBot()
        seen = []
        bot.set_command_handler(lambda text, chat_id, sender_id: seen.append((text, chat_id, sender_id)))

        bot._handle_event_line(json.dumps({
            "type": "im.message.receive_v1",
            "chat_id": "oc_123",
            "sender_id": "ou_456",
            "message_type": "text",
            "content": "@BotName 查额度",
        }, ensure_ascii=False))

        self.assertEqual(seen, [("查额度", "oc_123", "ou_456")])


if __name__ == "__main__":
    unittest.main()
