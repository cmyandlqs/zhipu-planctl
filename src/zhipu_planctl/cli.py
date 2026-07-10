"""CLI 入口：参数解析、信号处理、主循环调度。"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from typing import Optional

from .client import create_client
from .config import load_config
from .feishu_bot import FeishuBot
from .scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("zhipu-plan")


# 默认配置（找不到配置时的兜底）
_DEFAULT_COLD_START_TIMES = ["06:00", "11:00", "16:00", "21:00"]
_DEFAULT_QUOTA_INTERVAL_MIN = 5
_DEFAULT_COLD_START_MODEL = "glm-4.7"
_DEFAULT_COLD_START_PROMPT = "hi"

# 主循环 tick 间隔（秒）。Scheduler 内部有自己更细的时间判定。
_TICK_INTERVAL_SEC = 30


# ────────────────────────────  命令分发表  ────────────────────────────
# 命令别名集合（闭包 dispatch）。匹配顺序即优先级：新加命令时改这里即可。
_STATUS_ALIASES = frozenset({"查额度", "额度", "quota", "status", "状态"})
_COLD_START_ALIASES = frozenset({"冷启动", "cold start", "cold_start", "刷新", "重置"})
_HELP_ALIASES = frozenset({"help", "帮助", "菜单", "命令"})


def main():
    """主入口：解析参数 → 装载配置 → 组装对象 → 三种运行模式。

    运行模式：
    - --query：仅查询一次额度并打印 JSON；
    - --once：仅做一次冷启动并打印结果；
    - 默认（无 flag）：长跑，进入主循环（飞书 Bot + 调度器）。
    """
    parser = argparse.ArgumentParser(description="智谱 Coding Plan 自动管理工具")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="仅执行一次冷启动后退出")
    parser.add_argument("--query", action="store_true", help="仅查询额度后退出")
    args = parser.parse_args()

    cfg = load_config(args.config)
    s_cfg = cfg.get("schedule", {})
    f_cfg = cfg.get("feishu", {})

    try:
        client = create_client(cfg)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)

    if not client.api_key:
        log.error("请在配置文件中设置对应 provider 的 api_key")
        sys.exit(1)

    # 取当前 provider 的配置段
    provider = cfg.get("provider", "zhipu")
    p_cfg = cfg.get(provider, cfg)  # 向后兼容
    feishu = FeishuBot(
        notify_chat_id=f_cfg.get("notify_chat_id", ""),
        notify_threshold=f_cfg.get("notify_threshold", 0),
    )
    cold_start_times = s_cfg.get("cold_start_times", _DEFAULT_COLD_START_TIMES)
    quota_interval = s_cfg.get("quota_check_interval_minutes", _DEFAULT_QUOTA_INTERVAL_MIN)
    cs_model = p_cfg.get("cold_start_model", _DEFAULT_COLD_START_MODEL)
    cs_prompt = p_cfg.get("cold_start_prompt", _DEFAULT_COLD_START_PROMPT)
    scheduler = Scheduler(
        cold_start_times=cold_start_times,
        quota_check_interval_minutes=quota_interval,
    )

    # ─── 命令处理（内层函数：闭包捕获外部对象） ───

    def _cmd_status(chat_id: str):
        """『查额度』：拉一次额度，回送给发起命令的 chat。"""
        quota = scheduler.check_quota_now(client)
        feishu.send_status(quota, chat_id=chat_id, cold_start_times=cold_start_times)

    def _cmd_cold_start(chat_id: str):
        """『冷启动』：按需触发一次，回送结果给发起者。"""
        result = scheduler.cold_start_if_needed(client, model=cs_model, prompt=cs_prompt, force=True)
        if result["cold_started"]:
            feishu.send_message("✅ 冷启动成功", chat_id=chat_id)
        else:
            feishu.send_message(f"⏭️ {result['reason']}", chat_id=chat_id)

    def _cmd_help(chat_id: str):
        """『帮助』：列出可用命令。"""
        feishu.send_message(
            "🤖 可用命令:\n"
            "  查额度 / status  - 查看当前额度\n"
            "  冷启动 / refresh - 手动触发冷启动\n"
            "  帮助 / help      - 显示此菜单",
            chat_id=chat_id,
        )

    # 命令别名 → 处理函数。按声明顺序匹配，新加命令在这里加一行。
    _COMMANDS = {
        _STATUS_ALIASES: _cmd_status,
        _COLD_START_ALIASES: _cmd_cold_start,
        _HELP_ALIASES: _cmd_help,
    }

    def handle_command(text: str, chat_id: str, sender_id: str):
        """飞书命令路由：按别名表查表派发，未识别回提示给 chat_id。"""
        key = text.strip().lower()
        for aliases, handler in _COMMANDS.items():
            if key in aliases:
                handler(chat_id)
                return
        log.info("未知命令: %s", text)
        feishu.send_message(f"未识别命令: {text}", chat_id=chat_id)

    # ─── 一次性模式分支（不进主循环） ───

    if args.query:
        # 只查一次额度，输出 JSON 后退出
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
        # 只做一次冷启动，输出 JSON 后退出
        result = scheduler.cold_start_if_needed(client, model=cs_model, prompt=cs_prompt)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # ─── 守护模式：启动飞书 Bot 监听 + 主循环 ───

    feishu_bot_active = False
    if f_cfg.get("enable_bot", True) and FeishuBot.check_cli():
        feishu.set_command_handler(handle_command)
        feishu.start_listener()
        feishu_bot_active = True
        log.info("飞书 Bot 命令监听已启动")

    log.info("智谱 Coding Plan 管理工具已启动")
    log.info(f"冷启动时间: {', '.join(cold_start_times)}")
    log.info(f"额度查询间隔: {quota_interval} 分钟")
    log.info(f"飞书 Bot: {'已启用' if feishu_bot_active else '未启用'}")

    if feishu.notify_chat_id:
        feishu.send_message("🚀 智谱 Coding Plan 管理工具已启动")

    # 主循环：每次 sleep 后做一次 scheduler.tick()。
    # tick 内部根据当前时间槽决定是否触发冷启动或查额度。
    running = True

    def shutdown(_sig, _frame):
        """信号处理：把 running 置 False，让主循环在下一次 sleep 后退出。"""
        nonlocal running
        log.info("收到退出信号，正在关闭...")
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while running:
        time.sleep(_TICK_INTERVAL_SEC)
        if not running:
            break

        scheduler.tick(
            client=client,
            model=cs_model,
            prompt=cs_prompt,
            on_cold_start=lambda r: _on_cold_start(r, feishu, log, cold_start_times),
            on_quota=lambda q: _on_quota(q, feishu, log),
        )

    # 退出前清理：关飞书监听、刷一条日志
    feishu.stop()
    log.info("已停止")


def _on_cold_start(result: dict, feishu: FeishuBot, log: logging.Logger,
                   cold_start_times: Optional[list[str]] = None):
    """scheduler.tick 的冷启动完成回调：写日志 + 推到默认告警频道。"""
    if result["cold_started"]:
        log.info("冷启动成功")
        feishu.send_status(result["quota"], "✅ 冷启动成功",
                           cold_start_times=cold_start_times)
    else:
        reason = result["reason"]
        log.info(f"跳过冷启动: {reason}")
        if "失败" in reason:
            feishu.send_message(f"❌ 冷启动失败: {reason}")


def _on_quota(quota: dict, feishu: FeishuBot, log: logging.Logger):
    """scheduler.tick 的额度查询回调：写日志 + 触发阈值告警。"""
    if quota.get("ok"):
        pct = quota.get("five_hour_utilization")
        if pct is not None:
            log.info(f"额度: {pct:.1f}% | 重置: {quota.get('five_hour_resets_at', 'N/A')}")
        feishu.notify_quota(quota)


if __name__ == "__main__":
    main()
