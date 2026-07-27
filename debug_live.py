"""
实时端到端 debug：监听真实飞书事件 + 真实智谱 API + 真实回消息。

启动后会等 90 秒（或收到 1 条事件），你在飞书里给 bot 发消息，
脚本会把整个链路每一步的数据都打印出来，同时真把回复发到飞书。

用法：
    set PYTHONPATH=src && python debug_live.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from zhipu_planctl.config import load_config
from zhipu_planctl.client import create_client
from zhipu_planctl.scheduler import Scheduler
from zhipu_planctl.feishu_bot import FeishuBot
from zhipu_planctl.cli import CommandRouter, _COMMAND_MAP
import zhipu_planctl.feishu_bot as _fb_mod  # 用于 patch LARK_CLI

# Windows 下 subprocess 不走 shell，需要解析到带扩展名的全路径
LARK_CLI = shutil.which("lark-cli") or "lark-cli"
# 同步 patch 项目代码里的常量，让 FeishuBot.send_message 在 Windows 也能发
_fb_mod.LARK_CLI = LARK_CLI


def line(marker, text):
    print(f"\n[{time.strftime('%H:%M:%S')}] {marker}")
    if isinstance(text, (dict, list)):
        print(json.dumps(text, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)


def main():
    print("=" * 72)
    print("  实时飞书链路 debug  (90s / 1 条事件后自动退出)")
    print("=" * 72)

    cfg = load_config("config.yaml")
    client = create_client(cfg)
    scheduler = Scheduler(
        cold_start_times=cfg["schedule"]["cold_start_times"],
        quota_check_interval_minutes=cfg["schedule"]["quota_check_interval_minutes"],
    )
    feishu = FeishuBot(notify_chat_id="")

    # 包装 send_message：先打印再真发
    _real_send = feishu.send_message

    def traced_send(text, chat_id=None):
        line("📨 即将发送到飞书", f"chat_id={chat_id}\n内容:\n{text}")
        _real_send(text, chat_id=chat_id)
    feishu.send_message = traced_send

    router = CommandRouter(
        scheduler, client, feishu,
        cold_start_model=cfg["zhipu"]["cold_start_model"],
        cold_start_prompt=cfg["zhipu"]["cold_start_prompt"],
    )

    line("🔌", f"启动 {LARK_CLI} event consume (max-events=1, timeout=90s)")
    proc = subprocess.Popen(
        [
            LARK_CLI, "event", "consume", "im.message.receive_v1",
            "--max-events", "1",
            "--timeout", "90s",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
        encoding="utf-8",
    )

    print("\n" + "=" * 72)
    print("  👉 现在去飞书给 bot 发消息（90 秒内）：")
    print("     - 私聊 bot 发: 查额度")
    print("     - 或在群里 @bot 发: 查额度 / 冷启动 / 帮助 / 冷启动时间")
    print("  收到 1 条事件就自动退出并打印完整链路")
    print("=" * 72)

    try:
        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            # ─── 断点 A：lark-cli 推上来的原始 JSON ───
            line("📩 断点A: lark-cli 推上来的原始事件", raw_line)

            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as e:
                line("⚠️", f"非 JSON 行，跳过: {e}")
                continue

            if event.get("type") != "im.message.receive_v1":
                line("⏭️", f"非消息事件 (type={event.get('type')!r})，跳过")
                continue

            if event.get("message_type") != "text":
                line("⏭️", f"非文本消息 (message_type={event.get('message_type')!r})，跳过")
                continue

            chat_id = event.get("chat_id", "")
            sender_id = event.get("sender_id", "")
            content_raw = event.get("content", "")

            # ─── 断点 B：事件核心字段 ───
            line("🔍 断点B: 事件核心字段", {
                "chat_id": chat_id,
                "chat_type": event.get("chat_type"),
                "sender_id": sender_id,
                "message_id": event.get("message_id"),
                "content_raw": content_raw,
            })

            # 复刻 feishu_bot 的 content 清洗
            if isinstance(content_raw, dict):
                text = content_raw.get("text", "")
            elif isinstance(content_raw, str):
                try:
                    parsed = json.loads(content_raw)
                    text = parsed.get("text", "") if isinstance(parsed, dict) else content_raw
                except json.JSONDecodeError:
                    text = content_raw
            else:
                text = str(content_raw) if content_raw else ""

            original = text
            text = re.sub(r"^@\S+\s+", "", text).strip().lower()

            # ─── 断点 C：清洗后的 text + 命令分发 ───
            line("🧹 断点C: 文本清洗 + 命令分发表查找", {
                "原始": original,
                "剥@+lower后": text,
                "_COMMAND_MAP命中": _COMMAND_MAP.get(text, "<未命中/带参数命令>"),
            })

            if not text:
                line("⏭️", "清洗后为空，跳过")
                continue

            # ─── 断点 D：router.dispatch 内部会调真实 Zhipu API ───
            line("🎯 断点D: router.dispatch → 命中 handler → 调真实智谱 API",
                 f"dispatch(text={text!r}, chat_id={chat_id!r})")
            try:
                router.dispatch(text, chat_id, sender_id)
                line("✅", "本条事件处理完成")
            except Exception as e:
                line("❌", f"dispatch 异常: {e}")

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止...")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n已退出。")


if __name__ == "__main__":
    main()
