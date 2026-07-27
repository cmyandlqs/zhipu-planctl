import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from zhipu_planctl.config import load_config


class LoadConfigTest(unittest.TestCase):
    def test_env_api_key_overrides_file_but_preserves_schedule_and_feishu(self):
        config_text = textwrap.dedent("""
            provider: zhipu
            zhipu:
              api_key: file-key
              cold_start_model: glm-file
              cold_start_prompt: file-prompt
            schedule:
              cold_start_times:
                - "06:00"
                - "11:00"
                - "16:00"
                - "21:00"
              quota_check_interval_minutes: 5
            feishu:
              notify_chat_id: oc_xxx
              enable_bot: true
        """)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write(config_text)
            path = f.name

        try:
            with patch.dict(os.environ, {"ZHIPU_API_KEY": "env-key"}, clear=False):
                cfg = load_config(path)
        finally:
            os.remove(path)

        self.assertEqual(cfg["zhipu"]["api_key"], "env-key")
        self.assertEqual(cfg["schedule"]["cold_start_times"], ["06:00", "11:00", "16:00", "21:00"])
        self.assertEqual(cfg["feishu"]["notify_chat_id"], "oc_xxx")

    def test_env_only_still_loads_minimal_config(self):
        with patch.dict(os.environ, {"ZHIPU_API_KEY": "env-key"}, clear=True):
            cfg = load_config()

        self.assertEqual(cfg["provider"], "zhipu")
        self.assertEqual(cfg["zhipu"]["api_key"], "env-key")


if __name__ == "__main__":
    unittest.main()
