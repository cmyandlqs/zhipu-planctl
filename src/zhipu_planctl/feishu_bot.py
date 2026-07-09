"""飞书 Bot 封装。

- 通过 lark-cli（外部命令）发消息；
- 通过 lark-cli event consume 长连接接收消息事件。

不依赖任何额外 Python 包，进程外的 lark-cli 是唯一外部依赖。
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime
from typing import Callable, Optional


LARK_CLI = "lark-cli"   # 依赖宿主机的 lark-cli 命令（提前登录）


class FeishuBot:
    """飞书机器人主类。

    两种发送语义：
    - send_message / send_status / notify_quota → 默认收件人（config 里的 notify_chat_id，是"告警频道"）；
    - 显式传 chat_id 参数 → 一次性发到指定 chat（用于回复命令发起人），不修改实例状态。

    事件监听：
    - start_listener() 启动守护线程跑 lark-cli event consume，按行解析 JSON 事件；
    - 通过 set_command_handler 注册一个 (text, chat_id, sender_id) → None 的回调处理命令。
    """

    # lark-cli 调用的默认超时（秒）
    SEND_TIMEOUT = 15.0

    def __init__(self, notify_chat_id: str = "", notify_threshold: int = 0):
        # 默认告警频道（config 配的）；命令回复走 chat_id 参数，不改这里
        self.notify_chat_id = notify_chat_id
        # 已用百分比超过这个阈值才推送告警（0 = 关闭告警）
        self.notify_threshold = notify_threshold
        # 上一次推送过的百分比，用于避免重复通知
        self._last_notified_pct: Optional[float] = None
        self._event_proc: Optional[subprocess.Popen] = None
        self._command_handler: Optional[Callable] = None
        self._running = False

    # ─────────────────────── 发送消息 ───────────────────────

    def send_message(self, text: str, chat_id: Optional[str] = None):
        """发一条文本消息。

        chat_id 不传 → 默认发给 notify_chat_id（告警频道）；
        chat_id 传入 → 一次性发到指定 chat（不修改实例状态，适合回复命令）。
        """
        cid = chat_id or self.notify_chat_id
        if not cid:
            return
        try:
            subprocess.run(
                [LARK_CLI, "im", "send",
                 "--chat-id", cid,
                 "--msg-type", "text",
                 "--content", text],
                capture_output=True, timeout=self.SEND_TIMEOUT, text=True,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            # lark-cli 不可用时不抛，避免阻塞主循环
            pass

    def notify_quota(self, quota_data: dict):
        """额度告警：超过阈值时推一条到默认告警频道。同一百分比只发一次（防抖）。"""
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
        if pct < threshold:
            return
        # 防抖：只在新百分比与上一次不一样时才推
        if self._last_notified_pct is not None and pct == self._last_notified_pct:
            return

        reset = quota_data.get("five_hour_resets_at", "未知")
        self.send_message(
            f"⚠️ 智谱 Coding Plan 额度提醒\n"
            f"已用: {pct}%\n"
            f"重置时间: {reset}"
        )
        self._last_notified_pct = pct

    def send_status(self, quota_data: dict, cold_start_log: Optional[str] = None,
                    chat_id: Optional[str] = None):
        """把当前额度渲染成一条报告消息。

        chat_id 传了 → 发给指定 chat（回复命令发起人）；
        没传 → 发给默认告警频道。
        cold_start_log 是可选附加行，会拼接在状态后面。
        """
        target_chat_id = chat_id or self.notify_chat_id
        if not target_chat_id:
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
        self.send_message(msg, chat_id=target_chat_id)

    @staticmethod
    def _progress_bar(pct: float, length: int = 20) -> str:
        """把百分比渲染成 unicode 进度条。"""
        filled = int(pct / 100 * length)
        filled = max(0, min(filled, length))
        return "█" * filled + "░" * (length - filled)

    # ─────────────────────── 事件接收 ───────────────────────

    def _handle_event_line(self, line: str):
        """解析一行 lark-cli stdout 的 JSON 事件，转发给命令处理器。"""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        # 我们只关心收到消息这个事件类型
        if event.get("event") != "im.message.receive_v1":
            return

        data = event.get("data", {})
        # lark-cli 的 content 是字符串化的 JSON，需要二次解析
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
            # 派发到外部注册的命令处理函数
            self._command_handler(text, chat_id, sender_id)

    def set_command_handler(self, handler: Callable):
        """注册一个 (text, chat_id, sender_id) → None 的回调处理命令。"""
        self._command_handler = handler

    def start_listener(self):
        """启动守护线程，长连接订阅飞书消息事件。start_listener 本身幂等。"""
        if self._running:
            return
        self._running = True
        t = threading.Thread(target=self._listen_loop, daemon=True)
        t.start()

    def _listen_loop(self):
        """守护线程主循环：跑 lark-cli event consume，按行解析事件。"""
        try:
            self._event_proc = subprocess.Popen(
                [LARK_CLI, "event", "consume", "im.message.receive_v1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            # 按行迭代 stdout，每行是一个 JSON 事件
            for line in self._event_proc.stdout:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                self._handle_event_line(line)
        except FileNotFoundError:
            # lark-cli 不存在，跳过监听即可
            pass
        except Exception:
            # 任何其他异常都吞掉（守护线程不应该拖死主程序）
            pass

    def stop(self):
        """优雅停止监听：关掉标志 + terminate 子进程。"""
        self._running = False
        if self._event_proc:
            self._event_proc.terminate()
            self._event_proc = None

    @staticmethod
    def check_cli() -> bool:
        """检查 lark-cli 是否安装可用，用于决定要不要启用 Bot。"""
        try:
            subprocess.run([LARK_CLI, "--version"], capture_output=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
