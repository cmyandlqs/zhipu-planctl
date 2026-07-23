"""CLI 入口：参数解析、信号处理、主循环调度。"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from typing import Optional

import glob
import os
from functools import partial

from .client import create_client
from .config import load_config, CONFIG_PATHS
from .feishu_bot import FeishuBot
from .scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("zhipu-plan")


# 默认配置（找不到配置时的兜底）
_DEFAULT_COLD_START_TIMES = ["06:00", "16:00", "21:00"]
_DEFAULT_QUOTA_INTERVAL_MIN = 5
_DEFAULT_COLD_START_MODEL = "glm-4.7"
_DEFAULT_COLD_START_PROMPT = "hi"

# 主循环 tick 间隔（秒）。Scheduler 内部有自己更细的时间判定。
_TICK_INTERVAL_SEC = 30


# ────────────────────────────  命令分发表  ────────────────────────────
# 扁平 dict，每个别名映射到一个处理器
_COMMAND_MAP: dict[str, str] = {
    "查额度": "status", "额度": "status", "quota": "status", "status": "status", "状态": "status",
    "冷启动": "cold_start", "cold start": "cold_start", "cold_start": "cold_start",
    "刷新": "cold_start", "重置": "cold_start",
    "help": "help", "帮助": "help", "菜单": "help", "命令": "help",
    "改时间": "set_times", "settime": "set_times", "冷启动时间": "set_times",
}


def _setup_log_dir(log_dir: str):
    """创建日志目录并清理超过 24 小时的旧日志。"""
    os.makedirs(log_dir, exist_ok=True)
    cutoff = time.time() - 86400
    for old in glob.glob(os.path.join(log_dir, "zhipu-planctl-*.log")):
        try:
            if os.path.getmtime(old) < cutoff:
                os.remove(old)
                log.info("清理旧日志: %s", old)
        except OSError:
            pass


def _init_log_file(log_dir: str) -> logging.FileHandler:
    """创建当日日志文件 handler。"""
    today = time.strftime("%Y-%m-%d")
    log_path = os.path.join(log_dir, f"zhipu-planctl-{today}.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    return fh


def main():
    parser = argparse.ArgumentParser(description="智谱 Coding Plan 自动管理工具")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="仅执行一次冷启动后退出")
    parser.add_argument("--query", action="store_true", help="仅查询额度后退出")
    parser.add_argument("--log-dir", default="./logs", help="日志目录 (默认 ./logs)")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"zhipu-planctl {__version__}")
        sys.exit(0)

    if args.log_dir:
        _setup_log_dir(args.log_dir)
        fh = _init_log_file(args.log_dir)
        logging.getLogger().addHandler(fh)

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

    provider = cfg.get("provider", "zhipu")
    p_cfg = cfg.get(provider, {})
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

    # ─── 命令处理 ───

class CommandRouter:
    """命令路由器：分离命令解析、验证和执行逻辑，减少 cli.py 复杂度。"""

    def __init__(self, scheduler: "Scheduler", client: "BasePlanClient", feishu: "FeishuBot"):
        self.scheduler = scheduler
        self.client = client
        self.feishu = feishu

        self._handlers = {
            "status": self._handle_status,
            "cold_start": self._handle_cold_start,
            "help": self._handle_help,
            "set_times": self._handle_set_times,
        }

    def _handle_status(self, chat_id: str):
        quota = self.scheduler.check_quota_now(self.client)
        self.feishu.send_status(quota, chat_id=chat_id,
                                cold_start_times=self.scheduler.slots_as_strings())

    def _handle_cold_start(self, chat_id: str):
        result = self.scheduler.cold_start_if_needed(self.client, model=self._get_default_model(),
                                                     prompt=self._get_default_prompt(), force=True)
        if result["cold_started"]:
            self.feishu.send_message("✅ 冷启动成功", chat_id=chat_id)
        else:
            self.feishu.send_message(f"⏭️ {result['reason']}", chat_id=chat_id)

    def _handle_help(self, chat_id: str):
        self.feishu.send_message(
            "🤖 可用命令:\n"
            "  查额度 / status        - 查看当前额度\n"
            "  冷启动 / refresh       - 手动触发冷启动\n"
            "  冷启动时间 06:00 16:00 - 修改冷启动时间\n"
            "  帮助 / help            - 显示此菜单",
            chat_id=chat_id,
        )

    def _handle_set_times(self, chat_id: str, raw_text: str):
        parts = raw_text.strip().split()
        parts = parts[1:] if parts else []
        if not parts:
            self.feishu.send_message(
                f"⏰ 当前冷启动时间: {', '.join(self.scheduler.slots_as_strings())}\n"
                "修改格式: 冷启动时间 06:00 16:00 21:00",
                chat_id=chat_id)
            return
        try:
            new_times = []
            for p in parts:
                p = p.strip(",，;；")
                Scheduler._parse_time(p)  # Scheduler 是模块级静态方法
                new_times.append(p)
        except ValueError as e:
            self.feishu.send_message(f"❌ 时间格式错误: {e}", chat_id=chat_id)
            return
        self.scheduler.update_cold_start_times(new_times)
        log.info("冷启动时间已改为: %s", ", ".join(new_times))
        self.feishu.send_message(
            f"✅ 冷启动时间已改为: {', '.join(new_times)}",
            chat_id=chat_id)

    def _get_default_model(self) -> str:
        return "glm-4.7"  # 可后续从 config 读取

    def _get_default_prompt(self) -> str:
        return "hi"

    def dispatch(self, text: str, chat_id: str, sender_id: str):
        key = text.strip().lower()
        # set_times 命令带参数，特殊处理
        if key.startswith("冷启动时间") or key.split()[0] in ("改时间", "settime"):
            self._handle_set_times(chat_id, text.strip())
            return

        handler_key = _COMMAND_MAP.get(key)
        if handler_key is None:
            log.info("未知命令: %s", text)
            self.feishu.send_message(f"未识别命令: {text}", chat_id=chat_id)
            return

        handler = self._handlers.get(handler_key)
        if handler:
            handler(chat_id)

    # ─── 一次性模式 ───

    if args.query:
        result = client.query_quota()
        if result.ok:
            five_hour = next((t for t in result.tiers if t.name == "five_hour"), None)
            if five_hour:
                print(json.dumps({
                    "level": result.level,
                    "five_hour_utilization": five_hour.utilization,
                    "five_hour_resets_at": five_hour.resets_at,
                }, indent=2, ensure_ascii=False))
            else:
                print(json.dumps({
                    "level": result.level,
                    "ok": True,
                    "note": "无 five_hour 额度信息",
                }, indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"error": result.error}, indent=2, ensure_ascii=False))
        sys.exit(0 if result.ok else 1)

    if args.once:
        result = scheduler.cold_start_if_needed(client, model=cs_model,
                                                 prompt=cs_prompt, force=True)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # ─── 守护模式 ───

    feishu_bot_active = False
    if f_cfg.get("enable_bot", True) and FeishuBot.check_cli():
        feishu.set_command_handler(handle_command)
        feishu.start_listener()
        feishu_bot_active = True
        log.info("飞书 Bot 命令监听已启动")

    log.info("智谱 Coding Plan 管理工具已启动")
    log.info("冷启动时间: %s", ", ".join(scheduler.slots_as_strings()))
    log.info("额度查询间隔: %s 分钟", quota_interval)
    log.info("飞书 Bot: %s", "已启用" if feishu_bot_active else "未启用")
    log.info("日志目录: %s", args.log_dir)

    if feishu.notify_chat_id:
        feishu.send_message("🚀 智谱 Coding Plan 管理工具已启动")

    on_cs = partial(_on_cold_start, feishu=feishu, log=log)
    on_q = partial(_on_quota, feishu=feishu, log=log, scheduler=scheduler)

    scheduler.tick(client=client, model=cs_model, prompt=cs_prompt,
                   on_cold_start=on_cs, on_quota=on_q)

    running = True

    def shutdown(_sig, _frame):
        nonlocal running
        log.info("收到退出信号，正在关闭...")
        running = False

    def reload_config(_sig, _frame):
        nonlocal scheduler, quota_interval, cs_model, cs_prompt, client
        try:
            new_cfg = load_config(args.config)
            new_s_cfg = new_cfg.get("schedule", {})
            new_p_cfg = new_cfg.get(provider, {})
            new_provider = new_cfg.get("provider", "zhipu")

            if new_provider != provider:
                log.warning("config 热重载不支持切换 provider，忽略")
                return

            new_api_key = new_p_cfg.get("api_key", "")
            if new_api_key and new_api_key != client.api_key:
                try:
                    client = create_client(new_cfg)
                    log.info("API Key 已更新 (热重载)")
                except ValueError as e:
                    log.error("热重载创建 client 失败: %s", e)
                    return

            new_times = new_s_cfg.get("cold_start_times", _DEFAULT_COLD_START_TIMES)
            scheduler.update_cold_start_times(new_times)

            new_interval = new_s_cfg.get("quota_check_interval_minutes", _DEFAULT_QUOTA_INTERVAL_MIN)
            scheduler.update_quota_interval(new_interval)
            quota_interval = new_interval

            cs_model_new = new_p_cfg.get("cold_start_model", _DEFAULT_COLD_START_MODEL)
            cs_prompt_new = new_p_cfg.get("cold_start_prompt", _DEFAULT_COLD_START_PROMPT)
            cs_model = cs_model_new
            cs_prompt = cs_prompt_new

            log.info("配置已热重载 (冷启动: %s, 间隔: %smin)",
                     ", ".join(new_times), new_interval)
        except Exception:
            log.exception("热重载配置失败")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGHUP, reload_config)
    except AttributeError:
        pass

    _tick_chunks = _TICK_INTERVAL_SEC

    while running:
        for _ in range(_tick_chunks):
            if not running:
                break
            time.sleep(1)

        if not running:
            break

        try:
            scheduler.tick(client=client, model=cs_model, prompt=cs_prompt,
                           on_cold_start=on_cs, on_quota=on_q)
        except Exception:
            log.exception("tick() 异常")

    feishu.stop()
    log.info("已停止")


def _on_cold_start(result: dict, feishu: FeishuBot, log: logging.Logger):
    if result["cold_started"]:
        log.info("冷启动成功")
        feishu.send_status(result["quota"], "✅ 冷启动成功")
    else:
        reason = result["reason"]
        log.info("跳过冷启动: %s", reason)
        if "失败" in reason:
            feishu.send_message(f"❌ 冷启动失败: {reason}")


def _on_quota(quota: dict, feishu: FeishuBot, log: logging.Logger, scheduler):
    if quota.get("ok"):
        pct = quota.get("five_hour_utilization")
        if pct is not None:
            log.info("额度: %.1f%% | 重置: %s", pct, quota.get("five_hour_resets_at", "N/A"))
        feishu.notify_quota(quota)
        if quota.get("five_hour_expiring_soon"):
            reset_at = quota.get("five_hour_resets_at", "")
            if scheduler._last_window_warning != reset_at:
                scheduler._last_window_warning = reset_at
                log.info("⚠️ 窗口即将到期，%s", FeishuBot._to_beijing(reset_at))
                feishu.send_message(f"⏰ 5小时窗口将在 30 分钟内到期\n{FeishuBot._to_beijing(reset_at)} 重置")


if __name__ == "__main__":
    main()
