import logging.handlers
import os
import tempfile
import time
import unittest

from zhipu_planctl.cli import _init_log_file, _setup_log_dir


class LoggingTest(unittest.TestCase):
    def test_setup_log_dir_removes_files_older_than_retention(self):
        with tempfile.TemporaryDirectory() as d:
            old_path = os.path.join(d, "zhipu-planctl.log.2026-01-01")
            new_path = os.path.join(d, "zhipu-planctl.log")
            for path in (old_path, new_path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("log")

            old_mtime = time.time() - 49 * 3600
            os.utime(old_path, (old_mtime, old_mtime))

            _setup_log_dir(d, retention_hours=48)

            self.assertFalse(os.path.exists(old_path))
            self.assertTrue(os.path.exists(new_path))

    def test_init_log_file_uses_timed_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            handler = _init_log_file(d, retention_hours=48)
            try:
                self.assertIsInstance(handler, logging.handlers.TimedRotatingFileHandler)
                self.assertEqual(handler.when, "MIDNIGHT")
                self.assertTrue(handler.baseFilename.endswith("zhipu-planctl.log"))
            finally:
                handler.close()


if __name__ == "__main__":
    unittest.main()
