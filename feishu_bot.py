from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime
from typing import Optional, Callable


LARK_CLI = "lark-cli"


class FeishuBot:

    def __init__(self, notify_chat_id: str = "", notify_threshold: int = 0):
        self.notify_chat_id = notify_chat_id
        self.notify_threshold = notify_threshold
        self._last_notified_pct: Optional[float] = None
        self._event_proc: Optional[subprocess.Popen] = None
        self._command_handler: Optional[Callable] = None
        self._running = False

    def send_message(self, text: str, chat_id: Optional[str] = None):
        cid = chat_id or self.notify_chat_id
        if not cid:
            return
        try:
            subprocess.run(
                [LARK_CLI, "im", "send", "--chat-id", cid, "--msg-type", "text",
                 "--content", text],
                capture_output=True, timeout=15, text=True,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    def notify_quota(self, quota_data: dict):
        if not self.notify_chat_id:
            return
        if not quota_data.get("ok"):
            return

        pct = quota_data.get("five_hour_utilization")
        if pct is None:
            return
        pct = round(pct, 1)

        threshold = self.notify_threshold
        if threshold <= 0:
            return
        if pct >= threshold:
            if self._last_notified_pct is None or pct != self._last_notified_pct:
                reset = quota_data.get("five_hour_resets_at", "未知")
                self.send_message(
                    f"⚠️ 智谱 Coding Plan 额度提醒\n"
                    f"已用: {pct}%\n"
                    f"重置时间: {reset}"
                )
                self._last_notified_pct = pct

    def send_status(self, quota_data: dict, cold_start_log: Optional[str] = None):
        if not self.notify_chat_id:
            return

        if quota_data.get("ok"):
            pct = quota_data.get("five_hour_utilization", "N/A")
            reset = quota_data.get("five_hour_resets_at", "N/A")
            if isinstance(pct, float):
                bar = self._progress_bar(pct)
                msg = (
                    f"🤖 智谱 Coding Plan 状态\n"
                    f"套餐等级: {quota_data.get('level', 'N/A')}\n"
                    f"5小时窗口: {bar} {pct:.1f}%\n"
                    f"重置时间: {reset}"
                )
            else:
                msg = f"🤖 智谱 Coding Plan 状态\n查询失败: {quota_data.get('error', '未知')}"
        else:
            msg = f"❌ 查询失败: {quota_data.get('error', '未知')}"

        if cold_start_log:
            msg += f"\n{cold_start_log}"

        msg += f"\n查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        self.send_message(msg)

    @staticmethod
    def _progress_bar(pct: float, length: int = 20) -> str:
        filled = int(pct / 100 * length)
        filled = max(0, min(filled, length))
        return "█" * filled + "░" * (length - filled)

    def _handle_event_line(self, line: str):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        if event.get("event") != "im.message.receive_v1":
            return

        data = event.get("data", {})
        content_raw = data.get("content", "{}")
        chat_id = data.get("chat_id", "")
        sender_id = data.get("sender", {}).get("sender_id", {}).get("open_id", "")

        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
            text = content.get("text", "") if isinstance(content, dict) else str(content)
        except (json.JSONDecodeError, AttributeError):
            text = str(content_raw)

        text = text.strip().lower()

        if self._command_handler:
            self._command_handler(text, chat_id, sender_id)

    def set_command_handler(self, handler: Callable):
        self._command_handler = handler

    def start_listener(self):
        if self._running:
            return
        self._running = True
        t = threading.Thread(target=self._listen_loop, daemon=True)
        t.start()

    def _listen_loop(self):
        try:
            self._event_proc = subprocess.Popen(
                [LARK_CLI, "event", "consume", "im.message.receive_v1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )

            for line in self._event_proc.stdout:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                self._handle_event_line(line)

        except FileNotFoundError:
            pass
        except Exception:
            pass

    def stop(self):
        self._running = False
        if self._event_proc:
            self._event_proc.terminate()
            self._event_proc = None

    @staticmethod
    def check_cli() -> bool:
        try:
            subprocess.run([LARK_CLI, "--version"], capture_output=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
