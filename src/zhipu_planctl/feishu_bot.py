"""
飞书 Bot 封装。

核心设计：通过外部命令 lark-cli 进行所有飞书通信。

lark-cli 提供两个能力：
- 发送消息：lark-cli im +messages-send --chat-id <id> --text <text>
- 订阅事件：lark-cli event consume im.message.receive_v1（长连接，按行输出 JSON）

设计优势：
- 完全解耦飞书 SDK 的复杂性和版本依赖
- lark-cli 处理所有飞书 API 认证、事件格式、重连逻辑
- 本模块只通过 subprocess 调用 lark-cli，使用标准库即可

两种发送语义：
- send_message / send_status / notify_quota → 默认收件人（config 的 notify_chat_id）
- 显式传 chat_id → 一次性发到指定 chat（回复命令发起人），不修改实例状态

事件监听流程：
- start_listener() 启动守护线程跑 lark-cli event consume
- 按行解析 JSON 事件，调用注册的命令处理回调
- 通过 set_command_handler(handler) 注册回调函数
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional


LARK_CLI = "lark-cli"
_log = logging.getLogger("zhipu-plan.feishu")


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
    SEND_TIMEOUT = 30.0

    def __init__(self, notify_chat_id: str = "", notify_threshold: int = 0):
        """初始化飞书 Bot。

        Args:
            notify_chat_id: 默认告警频道（配置里的 notify_chat_id）。
                            主动通知（额度告警/到期告警/上线提示）都发到这个 chat。
            notify_threshold: 用量百分比告警阈值，>0 时启用，0 关闭告警。
        """
        # 默认告警频道（config 配的）；命令回复走 chat_id 参数，不改这里
        self.notify_chat_id = notify_chat_id
        # 已用百分比超过这个阈值才推送告警（0 = 关闭告警）
        self.notify_threshold = notify_threshold
        # 上一次推送过的百分比，用于避免重复通知
        self._last_notified_pct: Optional[float] = None
        # lark-cli event consume 子进程句柄（监听飞书消息事件）
        self._event_proc: Optional[subprocess.Popen] = None
        # 外部注册的命令处理回调，签名 (text, chat_id, sender_id) → None
        self._command_handler: Optional[Callable] = None
        # 监听线程运行标志：False 时主循环退出并终止子进程
        self._running = False

    # ─────────────────────── 发送消息 ───────────────────────

    def send_message(self, text: str, chat_id: Optional[str] = None):
        """发一条文本消息。

        chat_id 不传 → 默认发给 notify_chat_id（告警频道）；
        chat_id 传入 → 一次性发到指定 chat（不修改实例状态，适合回复命令）。

        Args:
            text: 要发送的纯文本内容
            chat_id: 可选目标会话；不传时用实例的 notify_chat_id
        """
        cid = chat_id or self.notify_chat_id
        if not cid:
            # 没配置目标 chat → 静默跳过（不是所有部署都会启用飞书）
            return
        try:
            # ─── 调用 lark-cli 发送消息 ───
            # start_new_session：让子进程脱离当前进程组，避免信号连坐
            # env：显式传 PATH/HOME，防止 systemd 环境下找不到 lark-cli
            result = subprocess.run(
                [LARK_CLI, "im", "+messages-send",
                 "--chat-id", cid,
                 "--text", text],
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
                timeout=self.SEND_TIMEOUT,
                start_new_session=True,
                env={**os.environ, "PATH": os.environ.get("PATH", ""),
                     "HOME": os.environ.get("HOME", "")},
            )
            if result.returncode != 0:
                _log.warning("lark-cli send failed rc=%d: %s", result.returncode, result.stderr.strip())
        except subprocess.TimeoutExpired:
            _log.warning("lark-cli send timeout")
        except FileNotFoundError:
            # lark-cli 未安装时单独提示，启动时 check_cli() 会再做一次
            _log.warning("lark-cli not found")
        except OSError as e:
            _log.warning("lark-cli send OS error: %s", e)

    def notify_quota(self, quota_data: dict):
        """额度告警：超过阈值时推一条到默认告警频道。同一百分比只发一次（防抖）。

        触发条件（全部满足才推送）：
        1. 配置了 notify_chat_id
        2. 查询成功（ok=True）
        3. 配置了阈值且阈值 > 0
        4. 当前百分比 ≥ 阈值
        5. 与上次推送的百分比不同（防抖，避免轮询时刷屏）

        Args:
            quota_data: Scheduler.check_quota_now 返回的标准化 dict
        """
        # ─── 前置条件检查：任意一个不满足就直接返回 ───
        if not self.notify_chat_id:
            return
        if not quota_data.get("ok"):
            return

        pct = quota_data.get("five_hour_utilization")
        if pct is None:
            return
        pct = round(pct, 1)  # 保留 1 位小数，避免浮点抖动导致防抖失效

        threshold = self.notify_threshold
        if threshold <= 0:
            return
        if pct < threshold:
            return
        # 防抖：只在新百分比与上一次不一样时才推
        if self._last_notified_pct is not None and pct == self._last_notified_pct:
            return

        # ─── 通过所有检查 → 推送并记录本次百分比 ───
        reset = self._to_beijing(quota_data.get("five_hour_resets_at", ""))
        self.send_message(
            f"⚠️ 智谱 Coding Plan 额度提醒\n"
            f"已用: {pct}%\n"
            f"{reset} 重置"
        )
        self._last_notified_pct = pct

    def send_status(self, quota_data: dict, cold_start_log: Optional[str] = None,
                    chat_id: Optional[str] = None,
                    cold_start_times: Optional[list[str]] = None):
        """把当前额度渲染成一条报告消息。

        chat_id 传了 → 发给指定 chat（回复命令发起人）；
        没传 → 发给默认告警频道。
        cold_start_log 是可选附加行，会拼接在状态后面。
        cold_start_times 是当天的冷启动时间节点列表，用于显示时间窗口。

        Args:
            quota_data: 标准化额度 dict（来自 Scheduler.check_quota_now）
            cold_start_log: 附加在前面的日志行（如 "✅ 冷启动成功"）
            chat_id: 可选目标会话；不传时用 notify_chat_id
            cold_start_times: 可选冷启动时间列表，用于显示 "⏰ 自动冷启动" 行
        """
        target_chat_id = chat_id or self.notify_chat_id
        if not target_chat_id:
            return

        # ─── 根据 ok 与数据是否可用渲染不同 emoji 的状态消息 ───
        if quota_data.get("ok"):
            pct = quota_data.get("five_hour_utilization", "N/A")
            reset_raw = quota_data.get("five_hour_resets_at", "")
            if isinstance(pct, float):
                # 颜色信号灯：<50% 绿 / <80% 黄 / 否则红
                emoji = "🟢" if pct < 50 else "🟡" if pct < 80 else "🔴"
                msg = (
                    f"{emoji} Coding Plan 状态\n"
                    f"5小时窗口: {pct:.1f}%"
                )
                if reset_raw:
                    msg += f"\n{self._to_beijing(reset_raw)} 重置"
                else:
                    msg += "\n重置时间未知"
            elif quota_data.get("level", ""):
                # OpenCode Go 等无 percentage 但有 level 说明的情况
                msg = f"🤖 Coding Plan 状态\n{quota_data['level']}"
            else:
                msg = f"🤖 Coding Plan 状态\n用量数据不可用"
        else:
            # 查询失败 → 红色叉，附带错误原因
            msg = f"❌ 查询失败: {quota_data.get('error', '未知')}"

        # ─── 追加可选的冷启动时间和日志行 ───
        if cold_start_times:
            msg += f"\n⏰ 自动冷启动: {', '.join(cold_start_times)}"
        if cold_start_log:
            msg += f"\n{cold_start_log}"
        self.send_message(msg, chat_id=target_chat_id)

    @staticmethod
    def _to_beijing(reset_iso: str) -> str:
        """UTC ISO 时间 → 北京时间 HH:MM。失败返回 "未知"。

        Args:
            reset_iso: UTC ISO 字符串，如 "2025-01-01T12:00:00+00:00"

        Returns:
            str: 北京时间 "HH:MM"（如 "20:00"），解析失败返回 "未知"
        """
        if not reset_iso:
            return "未知"
        try:
            t = reset_iso
            # ─── 兼容 Python < 3.11：把 "Z" 后缀替换成 "+00:00" ───
            if t.endswith("Z"):
                t = t[:-1] + "+00:00"
            utc_dt = datetime.fromisoformat(t)
            if utc_dt.tzinfo is None:
                # naive datetime 当作 UTC 处理
                utc_dt = utc_dt.replace(tzinfo=timezone.utc)
            # ─── 转换到东八区并格式化为 HH:MM ───
            bj_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
            return bj_dt.strftime("%H:%M")
        except (ValueError, OSError):
            return "未知"

    @staticmethod
    def _format_remaining(reset_iso: str) -> str:
        """把 UTC ISO 时间转成剩余时长字符串。

        输出格式：`XhYYmin`（≥1 小时）或 `Ymin`（不足 1 小时）。
        用于在终端展示"还剩多久重置"。

        Args:
            reset_iso: UTC ISO 格式的重置时间

        Returns:
            str: 剩余时长字符串，解析失败或已过期返回相应兜底值
        """
        if not reset_iso:
            return "未知"
        try:
            if reset_iso.endswith("Z"):
                reset_iso = reset_iso[:-1] + "+00:00"
            utc_dt = datetime.fromisoformat(reset_iso)
            if utc_dt.tzinfo is None:
                utc_dt = utc_dt.replace(tzinfo=timezone.utc)
            # ─── 统一在东八区计算差值 ───
            bj_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
            now = datetime.now(timezone(timedelta(hours=8)))
            remain = bj_dt - now
            total_minutes = remain.total_seconds() / 60
            if total_minutes <= 0:
                return "0min"
            hours = int(total_minutes // 60)
            minutes = int(total_minutes % 60)
            if hours > 0:
                return f"{hours}h{minutes:02d}min"
            return f"{minutes}min"
        except (ValueError, OSError):
            return "未知"

    # ─────────────────────── 事件接收 ───────────────────────

    def _handle_event_line(self, line: str):
        """解析一行 lark-cli stdout 的 JSON 事件，转发给命令处理器。

        lark-cli 实际事件 schema（实测，2025–2026）：
          {
            "type": "im.message.receive_v1",         # 不是 "event"
            "chat_id": "oc_...",
            "chat_type": "group" | "p2p",
            "sender_id": "ou_...",
            "message_id": "om_...",
            "content": "@BotName 查额度",               # 群聊中被 at，content 带 @ 前缀
            "message_type": "text"
          }

        注意字段全部是顶层平铺的，没有 data 包装层。

        Args:
            line: lark-cli stdout 的一行（理论上是一个 JSON 对象）
        """
        # ─── 步骤 1：解析 JSON ───
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # 非 JSON 行（如 lark-cli 自身的诊断输出）直接忽略
            return

        # 字段名是 type（不是 event）
        if event.get("type") != "im.message.receive_v1":
            return

        # 只处理文本消息（其他类型：image / file / post 等不在命令路由范围）
        if event.get("message_type") != "text":
            return

        chat_id = event.get("chat_id", "")
        sender_id = event.get("sender_id", "")

        # ─── 步骤 2：兼容两种 content 格式 ───
        # 实测：content 直接是字符串（如 "@BotName 查额度"），不是 JSON。
        # 老版 lark-cli 才会包成 {"text": "..."} 的 dict。为兼容两种情况都处理。
        content_raw = event.get("content", "")
        if isinstance(content_raw, dict):
            text = content_raw.get("text", "")
        elif isinstance(content_raw, str):
            # 试一下是不是 JSON 字符串，失败就当裸文本
            try:
                parsed = json.loads(content_raw)
                text = parsed.get("text", "") if isinstance(parsed, dict) else content_raw
            except json.JSONDecodeError:
                text = content_raw
        else:
            text = str(content_raw) if content_raw else ""

        if not isinstance(text, str):
            text = str(text)

        # ─── 步骤 3：清洗文本（剥 @ + lower） ───
        # 群聊里被 @机器人 会把 "@BotName " 拼到最前面，需要剥掉再分发。
        # 只移除第一个 mention token，避免把后面的真实命令一并吃掉。
        text = re.sub(r"^@\S+\s+", "", text)
        text = text.strip().lower()

        if not text:
            return

        # ─── 步骤 4：派发给外部注册的命令处理函数 ───
        if self._command_handler:
            # 派发到外部注册的命令处理函数
            self._command_handler(text, chat_id, sender_id)

    def set_command_handler(self, handler: Callable):
        """注册一个 (text, chat_id, sender_id) → None 的回调处理命令。

        注册后，每收到一条文本消息事件就会派发到 handler。
        典型用法：由 cli.py 注册一个调用 CommandRouter.dispatch 的闭包。

        Args:
            handler: 命令处理回调函数
        """
        self._command_handler = handler

    def start_listener(self):
        """启动守护线程，长连接订阅飞书消息事件。start_listener 本身幂等。

        幂等性：重复调用不会启动多个线程（靠 self._running 标志判断）。
        实际的事件解析和派发在 _listen_loop 中进行。
        """
        if self._running:
            return  # 已经在跑，避免重复启动
        self._running = True
        # daemon=True：主进程退出时线程自动结束，不会阻塞关闭
        t = threading.Thread(target=self._listen_loop, daemon=True)
        t.start()

    def _listen_loop(self):
        """守护线程主循环：跑 lark-cli event consume，按行解析事件。

        子进程退出后自动重连，直到 self._running 为 False。

        关键点：lark-cli 看到 stdin EOF 就自退出，所以必须传
        stdin=subprocess.PIPE 让 Python 持有 stdin 不关。

        重连策略：子进程异常退出后 sleep 5 秒再拉起，避免在 lark-cli 持续
        失败时打爆 CPU。
        """
        while self._running:
            try:
                # ─── 启动 lark-cli 事件订阅子进程 ───
                # bufsize=1：行缓冲，配合 text=True 逐行读取
                self._event_proc = subprocess.Popen(
                    [LARK_CLI, "event", "consume", "im.message.receive_v1"],
                    stdin=subprocess.PIPE,   # 必须持有 stdin，见函数 docstring
                    stdout=subprocess.PIPE,  # 按行读取事件 JSON
                    stderr=subprocess.DEVNULL,
                    text=True, bufsize=1,
                )
                _log.debug("event consumer started pid=%d", self._event_proc.pid)
                # ─── 逐行读取事件并派发 ───
                for line in self._event_proc.stdout:
                    if not self._running:
                        break
                    line = line.strip()
                    _log.debug("raw event: %s", line)
                    if not line:
                        continue
                    self._handle_event_line(line)
            except FileNotFoundError:
                # lark-cli 完全没装 → 没必要重连，直接退出循环
                break
            except Exception as e:
                _log.exception("listen_loop exception")
            finally:
                # ─── 无论怎么退出，都先把子进程干净地终止 ───
                if self._event_proc:
                    self._event_proc.terminate()
                    self._event_proc = None
            # ─── 还在运行就等 5 秒后重连 ───
            if self._running:
                _time.sleep(5)
        _log.debug("event consumer loop exited")

    def stop(self):
        """优雅停止监听：关掉标志 + terminate 子进程。

        由 cli.py 的关闭流程调用，先翻 running 标志让循环自然退出，
        同时 terminate 子进程避免被 sleep 卡住。
        """
        self._running = False
        if self._event_proc:
            self._event_proc.terminate()
            self._event_proc = None

    @staticmethod
    def check_cli() -> bool:
        """检查 lark-cli 是否安装可用，用于决定要不要启用 Bot。

        cli.py 在启动监听前调用一次：返回 False 时跳过飞书监听，
        让守护进程在没有 lark-cli 的环境下也能跑（只是失去远程命令能力）。

        Returns:
            bool: True 表示 lark-cli 可调用
        """
        try:
            subprocess.run([LARK_CLI, "--version"], capture_output=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
