from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

import yaml

from zhipu_client import ZhipuClient
from scheduler import Scheduler
from feishu_bot import FeishuBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("zhipu-plan")

CONFIG_PATHS = [
    "./config.yaml",
    os.path.expanduser("~/.config/zhipu-plan/config.yaml"),
    os.path.expanduser("~/.zhipu-plan.yaml"),
]


def load_config(path: str | None = None) -> dict:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    for cp in CONFIG_PATHS:
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                return yaml.safe_load(f)

    raise FileNotFoundError(
        "未找到配置文件。请创建 config.yaml，参考 config.yaml.example"
    )


def main():
    parser = argparse.ArgumentParser(description="智谱 Coding Plan 自动管理工具")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="仅执行一次冷启动后退出")
    parser.add_argument("--query", action="store_true", help="仅查询额度后退出")
    args = parser.parse_args()

    cfg = load_config(args.config)
    z_cfg = cfg.get("zhipu", {})
    s_cfg = cfg.get("schedule", {})
    f_cfg = cfg.get("feishu", {})

    if not z_cfg.get("api_key"):
        log.error("请在配置文件中设置 zhipu.api_key")
        sys.exit(1)

    client = ZhipuClient(
        api_key=z_cfg["api_key"],
        base_url=z_cfg.get("base_url", "https://open.bigmodel.cn"),
    )

    feishu = FeishuBot(
        notify_chat_id=f_cfg.get("notify_chat_id", ""),
        notify_threshold=f_cfg.get("notify_threshold", 0),
    )

    cold_start_times = s_cfg.get("cold_start_times", ["07:00", "12:00", "17:00", "22:00"])
    quota_interval = s_cfg.get("quota_check_interval_minutes", 5)
    cs_model = z_cfg.get("cold_start_model", "glm-4-air")
    cs_prompt = z_cfg.get("cold_start_prompt", "hi")

    def handle_command(text: str, chat_id: str, sender_id: str):
        cmd = text.strip().lower()

        if cmd in ("查额度", "额度", "quota", "status", "状态"):
            log.info(f"飞书命令: {text}")
            quota = scheduler.check_quota_now(client)
            feishu.notify_chat_id = chat_id
            feishu.send_status(quota)

        elif cmd in ("冷启动", "cold start", "cold_start", "刷新", "重置"):
            log.info(f"飞书命令: {text}")
            result = scheduler.cold_start_if_needed(client, model=cs_model, prompt=cs_prompt)
            if result["cold_started"]:
                feishu.send_message("✅ 冷启动成功", chat_id=chat_id)
            else:
                feishu.send_message(f"⏭️ {result['reason']}", chat_id=chat_id)

        elif cmd in ("help", "帮助", "菜单", "命令"):
            feishu.send_message(
                "🤖 可用命令:\n"
                "  查额度 / status  - 查看当前额度\n"
                "  冷启动 / refresh - 手动触发冷启动\n"
                "  帮助 / help      - 显示此菜单",
                chat_id=chat_id,
            )

    if args.query:
        result = client.query_quota()
        if result.ok:
            for t in result.tiers:
                if t.name == "five_hour":
                    print(json.dumps({
                        "level": result.level,
                        "five_hour_utilization": t.utilization,
                        "five_hour_resets_at": t.resets_at,
                    }, indent=2, ensure_ascii=False))
                    break
        else:
            print(json.dumps({"error": result.error}, indent=2, ensure_ascii=False))
        return

    if args.once:
        result = scheduler.cold_start_if_needed(client, model=cs_model, prompt=cs_prompt)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    feishu_bot_active = False
    if f_cfg.get("enable_bot", True) and FeishuBot.check_cli():
        feishu.set_command_handler(handle_command)
        feishu.start_listener()
        feishu_bot_active = True
        log.info("飞书 Bot 命令监听已启动")

    scheduler.__class__._processed_slots = set()

    log.info("智谱 Coding Plan 管理工具已启动")
    log.info(f"冷启动时间: {', '.join(cold_start_times)}")
    log.info(f"额度查询间隔: {quota_interval} 分钟")
    log.info(f"飞书 Bot: {'已启用' if feishu_bot_active else '未启用'}")

    if feishu.notify_chat_id:
        feishu.send_message("🚀 智谱 Coding Plan 管理工具已启动")

    running = True

    def shutdown(sig, frame):
        nonlocal running
        log.info("收到退出信号，正在关闭...")
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    SLEEP_INTERVAL = 30

    while running:
        time.sleep(SLEEP_INTERVAL)

        if not running:
            break

        scheduler.tick(
            zhipu_client=client,
            model=cs_model,
            prompt=cs_prompt,
            on_cold_start=lambda r: _on_cold_start(r, feishu, log),
            on_quota=lambda q: _on_quota(q, feishu, log),
        )

    feishu.stop()
    log.info("已停止")


def _on_cold_start(result: dict, feishu: FeishuBot, log: logging.Logger):
    if result["cold_started"]:
        log.info("冷启动成功")
        feishu.send_status(result["quota"], "✅ 冷启动成功")
    else:
        reason = result["reason"]
        log.info(f"跳过冷启动: {reason}")
        if "失败" in reason:
            feishu.send_message(f"❌ 冷启动失败: {reason}")


def _on_quota(quota: dict, feishu: FeishuBot, log: logging.Logger):
    if quota.get("ok"):
        pct = quota.get("five_hour_utilization")
        if pct is not None:
            log.info(f"额度: {pct:.1f}% | 重置: {quota.get('five_hour_resets_at', 'N/A')}")
        feishu.notify_quota(quota)


if __name__ == "__main__":
    main()
