"""
CLI 入口：参数解析、信号处理、主循环调度。

核心职责：
- 解析命令行参数（--query / --once / --watch / --log-dir 等）
- 加载配置、创建厂商客户端、调度器、飞书 Bot 实例
- 注册飞书命令处理器，把机器人消息路由到对应动作
- 维护守护主循环，每 _TICK_INTERVAL_SEC 秒推进一次 Scheduler.tick
- 处理 SIGTERM/SIGINT 优雅退出、SIGHUP 热重载配置

设计特点：
- 副作用集中在 main() 内的回调（_on_cold_start / _on_quota），Scheduler 本身保持纯粹
- 命令路由抽到 CommandRouter 类，避免 main() 膨胀
- 日志按天轮转 + mtime 保留策略，适配 systemd 长期运行
- 信号回调只翻转 running 标志，主循环负责实际清理

运行模式：
- 默认守护模式：常驻调度 + 飞书监听（生产部署用）
- --query：查一次额度并打印 JSON 后退出（脚本/调试用）
- --once：执行一次冷启动后退出（手动激活用）
- --watch：终端实时仪表盘（开发观察用）
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import time
from typing import Optional

import glob
import os
from functools import partial

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


# 默认配置（config.yaml 缺失对应字段时的兜底值）
# 注意：_DEFAULT_COLD_START_TIMES 覆盖 06-11 / 11-16 / 16-21 / 21-02 四个 5 小时窗口
# 不要随意加 02:00 槽位，详见 AGENTS.md
_DEFAULT_COLD_START_TIMES = ["06:00", "11:00", "16:00", "21:00"]
_DEFAULT_QUOTA_INTERVAL_MIN = 5        # 额度轮询间隔（分钟）
_DEFAULT_COLD_START_MODEL = "glm-4.7"  # 冷启动使用的默认模型
_DEFAULT_COLD_START_PROMPT = "hi"      # 冷启动发送的默认 prompt（极短，省 token）
_DEFAULT_LOG_RETENTION_HOURS = 48      # 日志默认保留时长（小时）

# 主循环 tick 间隔（秒）。Scheduler 内部有自己更细的时间判定（±1min 容错），
# 这里只控制"多久看一次表"，30 秒足够灵敏且不会打爆 API。
_TICK_INTERVAL_SEC = 30


# ────────────────────────────  命令分发表  ────────────────────────────
# 扁平 dict：把所有别名（中英文、口语化）归一到一个标准 handler key。
# CommandRouter._handlers 再把标准 key 映射到具体处理函数。
# 新增命令别名只需在此追加，无需改动路由逻辑。
_COMMAND_MAP: dict[str, str] = {
    # 查额度类 → status
    "查额度": "status", "额度": "status", "quota": "status", "status": "status", "状态": "status",
    # 冷启动类 → cold_start
    "冷启动": "cold_start", "cold start": "cold_start", "cold_start": "cold_start", "refresh": "cold_start",
    "刷新": "cold_start", "重置": "cold_start",
    # 帮助类 → help
    "help": "help", "帮助": "help", "菜单": "help", "命令": "help",
    # 改冷启动时间 → set_times（带参数，dispatch 中特殊处理）
    "改时间": "set_times", "settime": "set_times", "冷启动时间": "set_times",
}


def _setup_log_dir(log_dir: str, retention_hours: int = _DEFAULT_LOG_RETENTION_HOURS):
    """创建日志目录并清理超过 retention_hours 的旧日志。

    在启动早期调用一次，保证后续写日志时目录一定存在，
    同时把上次运行残留的过期日志先清掉。

    Args:
        log_dir: 日志目录路径
        retention_hours: 日志保留小时数，早于该时长的文件会被删除
    """
    os.makedirs(log_dir, exist_ok=True)
    # cutoff 是"保留边界"的时间戳：早于此值的文件视为过期
    cutoff = time.time() - max(retention_hours, 1) * 3600
    # 同时匹配两种历史命名：当前主日志 + 早期版本带日期后缀的文件
    for pattern in ("zhipu-planctl.log*", "zhipu-planctl-*.log"):
        old_logs = glob.glob(os.path.join(log_dir, pattern))
        for old in old_logs:
            _remove_old_log(old, cutoff)


def _remove_old_log(path: str, cutoff: float):
    """删除单个过期日志文件（mtime 早于 cutoff）。

    静默吞掉 OSError：日志清理失败不应影响主流程运行。
    """
    try:
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            log.info("清理旧日志: %s", path)
    except OSError:
        pass


class RetentionTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """按天轮转 + 基于 mtime 保留期的日志 handler。

    标准库 TimedRotatingFileHandler 只按 backupCount 保留固定份数，
    在跨天频繁轮转或机器时间跳变时容易误删/堆积。
    本类重写 doRollover，每次轮转后额外按 mtime 清理过期日志，
    适配 systemd 24 小时常驻场景（与 LINUX_DEPLOYMENT.md 一致）。
    """

    def __init__(self, filename: str, retention_hours: int, **kwargs):
        """初始化 handler。

        Args:
            filename: 当前日志文件路径（如 logs/zhipu-planctl.log）
            retention_hours: 保留小时数，轮转后据此清理
            **kwargs: 透传给 TimedRotatingFileHandler（when/interval/encoding 等）
        """
        super().__init__(filename, **kwargs)
        self.retention_hours = retention_hours

    def doRollover(self):
        """轮转后追加一次基于 mtime 的清理。"""
        super().doRollover()
        cutoff = time.time() - max(self.retention_hours, 1) * 3600
        log_dir = os.path.dirname(self.baseFilename)
        # 只清本 handler 管辖的主日志历史副本，避免误删其他文件
        for old in glob.glob(os.path.join(log_dir, "zhipu-planctl.log*")):
            _remove_old_log(old, cutoff)


def _init_log_file(log_dir: str,
                   retention_hours: int = _DEFAULT_LOG_RETENTION_HOURS) -> logging.Handler:
    """创建按天轮转的日志文件 handler，适合 systemd 下长期运行。

    轮转时机：每天午夜（when="midnight"）。
    文件命名：当前为 zhipu-planctl.log，历史为 zhipu-planctl.log.YYYY-MM-DD。

    Args:
        log_dir: 日志目录
        retention_hours: 保留小时数（透传给 RetentionTimedRotatingFileHandler）

    Returns:
        logging.Handler: 配好的 handler，由调用方挂到 root logger
    """
    log_path = os.path.join(log_dir, "zhipu-planctl.log")
    fh = RetentionTimedRotatingFileHandler(
        log_path,
        retention_hours=retention_hours,
        when="midnight",     # 跨午夜触发轮转
        interval=1,          # 每 1 天一次
        backupCount=0,       # 不靠份数，靠 mtime 清理（见 doRollover）
        encoding="utf-8",
    )
    # 历史文件后缀格式：zhipu-planctl.log.2025-01-01
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(logging.DEBUG)  # 文件记录全量 DEBUG，stdout 由 basicConfig 控制 INFO
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    return fh


class CommandRouter:
    """命令路由器：分离命令解析、验证和执行逻辑，减少 cli.py 复杂度。

    设计目的：
    - 把飞书消息 → 动作的映射从 main() 抽出，便于单测
    - 持有 scheduler/client/feishu 三大依赖，handler 内部直接调用
    - 命令别名表（_COMMAND_MAP）与具体 handler 解耦，新增别名不改路由代码

    路由流程：
        飞书消息 text
            ↓
        dispatch(text, chat_id, sender_id)
            ↓
        归一化为 handler key（status/cold_start/help/set_times）
            ↓
        查 _handlers 字典调用对应 _handle_xxx 方法
    """

    def __init__(self, scheduler: "Scheduler", client: "BasePlanClient", feishu: "FeishuBot",
                 cold_start_model: str, cold_start_prompt: str):
        """初始化路由器，绑定三大依赖与冷启动默认参数。

        Args:
            scheduler: 调度器实例（用于查额度、冷启动、改时间）
            client: 厂商客户端实例
            feishu: 飞书 Bot 实例（用于回复消息）
            cold_start_model: 冷启动使用的模型名（可被热重载更新）
            cold_start_prompt: 冷启动发送的 prompt（可被热重载更新）
        """
        self.scheduler = scheduler
        self.client = client
        self.feishu = feishu
        self.cold_start_model = cold_start_model
        self.cold_start_prompt = cold_start_prompt

        # 标准 handler key → 处理方法的映射表
        # 与 _COMMAND_MAP 的 value 一一对应
        self._handlers = {
            "status": self._handle_status,
            "cold_start": self._handle_cold_start,
            "help": self._handle_help,
            "set_times": self._handle_set_times,
        }

    def _handle_status(self, chat_id: str):
        """处理 status 命令：查一次额度并回复当前状态。"""
        quota = self.scheduler.check_quota_now(self.client)
        # 回复到命令发起的 chat（chat_id 是发起人所在会话）
        self.feishu.send_status(quota, chat_id=chat_id,
                                cold_start_times=self.scheduler.slots_as_strings())

    def _handle_cold_start(self, chat_id: str):
        """处理 cold_start 命令：手动触发一次冷启动（force=True 带重试验证）。

        无论成功/失败/跳过都回复用户，便于远程确认状态。
        """
        result = self.scheduler.cold_start_if_needed(self.client, model=self.cold_start_model,
                                                     prompt=self.cold_start_prompt, force=True)
        if result["cold_started"]:
            self.feishu.send_message("✅ 冷启动成功", chat_id=chat_id)
        else:
            # 跳过（窗口未过期）或失败（重试耗尽）都走这里，reason 中会区分
            self.feishu.send_message(f"⏭️ {result['reason']}", chat_id=chat_id)

    def _handle_help(self, chat_id: str):
        """处理 help 命令：返回可用命令清单。"""
        self.feishu.send_message(
            "🤖 可用命令:\n"
            "  查额度 / status        - 查看当前额度\n"
            "  冷启动 / refresh       - 手动触发冷启动\n"
            "  冷启动时间 06:00 11:00 16:00 21:00 - 修改冷启动时间\n"
            "  帮助 / help            - 显示此菜单",
            chat_id=chat_id,
        )

    def _handle_set_times(self, chat_id: str, raw_text: str):
        """处理 set_times 命令：修改冷启动时间。

        支持两种形式：
        - 无参数 → 回显当前时间，提示格式
        - 带参数 → 解析并更新 scheduler（同时写入运行时状态，SIGHUP 时也会重新加载）

        格式错误会逐项校验并提示，不会部分写入。
        """
        # ─── 拆分参数：第一个 token 是命令本身，后面才是时间 ───
        parts = raw_text.strip().split()
        parts = parts[1:] if parts else []

        if not parts:
            # ─── 无参数 → 仅回显当前配置和用法 ───
            self.feishu.send_message(
                f"⏰ 当前冷启动时间: {', '.join(self.scheduler.slots_as_strings())}\n"
                "修改格式: 冷启动时间 06:00 11:00 16:00 21:00",
                chat_id=chat_id)
            return

        # ─── 解析每个时间 token，遇到非法立即报错返回 ───
        try:
            new_times = []
            for p in parts:
                # 容错：剥掉用户可能带的逗号/分号/中文标点分隔符
                p = p.strip(",，;；")
                Scheduler._parse_time(p)  # 复用 scheduler 的校验逻辑（格式 + 范围）
                new_times.append(p)
        except ValueError as e:
            self.feishu.send_message(f"❌ 时间格式错误: {e}", chat_id=chat_id)
            return

        # ─── 校验通过 → 更新调度器并记录日志 ───
        self.scheduler.update_cold_start_times(new_times)
        log.info("冷启动时间已改为: %s", ", ".join(new_times))
        self.feishu.send_message(
            f"✅ 冷启动时间已改为: {', '.join(new_times)}",
            chat_id=chat_id)

    def update_cold_start_defaults(self, model: str, prompt: str):
        """热重载时更新冷启动的模型与 prompt（保持 router 与 scheduler 配置一致）。"""
        self.cold_start_model = model
        self.cold_start_prompt = prompt

    def dispatch(self, text: str, chat_id: str, sender_id: str):
        """把飞书消息文本路由到对应 handler。

        解析顺序（先匹配先生效）：
        1. set_times 命令带参数，按关键词特殊匹配（冷启动时间/改时间/settime）
        2. 整条文本作为别名查 _COMMAND_MAP（最常见路径）
        3. 兜底：剥离飞书 @ 机器人后残留的卡片前缀，按最后一个词再匹配一次

        Args:
            text: 经过 feishu_bot 清洗后的消息文本（已 lower、已剥 @）
            chat_id: 回复目标会话
            sender_id: 发送人 open_id（预留扩展用）
        """
        raw = text.strip()
        key = raw.lower()
        if not key:
            return

        # ─── 分支 1：set_times 带参数，单独处理（因为要取参数） ───
        if "冷启动时间" in key:
            # 截取从关键词开始的子串，交给 handler 再解析
            self._handle_set_times(chat_id, raw[key.index("冷启动时间"):])
            return
        words = key.split()
        for marker in ("改时间", "settime"):
            if marker in words:
                self._handle_set_times(chat_id, " ".join(words[words.index(marker):]))
                return

        # ─── 分支 2：整条文本查别名表 ───
        handler_key = _COMMAND_MAP.get(key)

        if handler_key is None:
            # ─── 分支 3：兜底，处理 "xxx 查额度" 这种前缀残留 ───
            # 兼容飞书 mention 剥离不完整时留下的 bot 名片后缀，如 "cli 查额度"。
            # 按别名长度倒序匹配，避免短别名误中长别名子串。
            for alias, mapped in sorted(_COMMAND_MAP.items(), key=lambda item: len(item[0]), reverse=True):
                if key.endswith(f" {alias}") or words[-1] == alias:
                    handler_key = mapped
                    break

        if handler_key is None:
            # ─── 未识别命令 → 回复提示，避免用户以为机器人没响应 ───
            log.info("未知命令: %s", text)
            self.feishu.send_message(f"未识别命令: {text}", chat_id=chat_id)
            return

        # ─── 命中 handler → 调用对应方法 ───
        handler = self._handlers.get(handler_key)
        if handler:
            handler(chat_id)


def main():
    """CLI 主入口：参数解析 → 装配依赖 → 进入指定运行模式。

    运行模式（互斥）：
    - --version：打印版本后立即退出
    - --query：查一次额度打印 JSON 后退出
    - --once：执行一次冷启动后退出
    - --watch：进入终端实时仪表盘循环
    - 默认：进入守护模式（调度 + 飞书监听 + 信号处理）

    守护模式下会注册三个信号：
    - SIGTERM / SIGINT：优雅退出（翻转 running 标志）
    - SIGHUP：热重载配置（cold_start_times / interval / api_key / model / prompt）
    """
    # ─── 步骤 1：解析命令行参数 ───
    # python -m zhipu_planctl --query
    parser = argparse.ArgumentParser(description="智谱 Coding Plan 自动管理工具")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="仅执行一次冷启动后退出")# 布尔开关，这个参数不带值，只要它"出现"就存 True，"没出现"就存 False
    parser.add_argument("--query", action="store_true", help="仅查询额度后退出")
    parser.add_argument("--log-dir", default="./logs", help="日志目录 (默认 ./logs)")
    parser.add_argument("--log-retention-hours", type=int, default=_DEFAULT_LOG_RETENTION_HOURS,
                        help="日志保留小时数 (默认 48)")
    parser.add_argument("--version", action="store_true", help="显示版本")
    parser.add_argument("--watch", action="store_true", help="实时仪表盘模式（终端显示额度、剩余时间等）")

    # add_argument用来声明
    # parse_args用来解析
    args = parser.parse_args() # 返回一个干净的对象，其中的属性就是前面解析的参数

    # ─── 步骤 2：--version 提前退出 ───
    if args.version:
        from . import __version__
        print(f"zhipu-planctl {__version__}")
        sys.exit(0)

    # ─── 步骤 3：初始化文件日志（守护模式必需） ───
    if args.log_dir:
        _setup_log_dir(args.log_dir, args.log_retention_hours)
        fh = _init_log_file(args.log_dir, args.log_retention_hours)
        logging.getLogger().addHandler(fh)

    # ─── 步骤 4：加载配置 + 创建三大组件 ───
    cfg = load_config(args.config)
    s_cfg = cfg.get("schedule", {})   # 调度相关配置
    f_cfg = cfg.get("feishu", {})     # 飞书相关配置

    # ─── 步骤 4.1：创建厂商客户端 ───
    try:
        client = create_client(cfg)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)

    # API Key 必须存在，否则后续所有请求都会失败
    if not client.api_key:
        log.error("请在配置文件中设置对应 provider 的 api_key")
        sys.exit(1)

    # ─── 步骤 4.2：按 provider 取厂商专属配置（model/prompt 等） ───
    provider = cfg.get("provider", "zhipu")
    p_cfg = cfg.get(provider, {})

    # ─── 步骤 4.3：实例化飞书 Bot（即使未启用，对象本身可以先建好） ───
    feishu = FeishuBot(
        notify_chat_id=f_cfg.get("notify_chat_id", ""),
        notify_threshold=f_cfg.get("notify_threshold", 0),
    )

    # ─── 步骤 4.4：实例化调度器（核心调度逻辑都在这里） ───
    cold_start_times = s_cfg.get("cold_start_times", _DEFAULT_COLD_START_TIMES)
    quota_interval = s_cfg.get("quota_check_interval_minutes", _DEFAULT_QUOTA_INTERVAL_MIN)
    cs_model = p_cfg.get("cold_start_model", _DEFAULT_COLD_START_MODEL)
    cs_prompt = p_cfg.get("cold_start_prompt", _DEFAULT_COLD_START_PROMPT)
    scheduler = Scheduler(
        cold_start_times=cold_start_times,
        quota_check_interval_minutes=quota_interval,
    )

    # ─── 命令处理：装配 CommandRouter + 适配 feishu_bot 回调签名 ───
    # FeishuBot 期望的回调是 (text, chat_id, sender_id)，router.dispatch 正好对应

    router = CommandRouter(scheduler, client, feishu,
                           cold_start_model=cs_model,
                           cold_start_prompt=cs_prompt)

    def handle_command(text: str, chat_id: str, sender_id: str):
        router.dispatch(text, chat_id, sender_id)

    # ─── 一次性模式：--query / --once 都会在完成任务后退出 ───

    if args.query:
        # ─── 查额度模式：打印 JSON 后退出，退出码反映查询是否成功 ───
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
                # 查询成功但没有 five_hour 层级（如 OpenCode Go），给出说明
                print(json.dumps({
                    "level": result.level,
                    "ok": True,
                    "note": "无 five_hour 额度信息",
                }, indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"error": result.error}, indent=2, ensure_ascii=False))
        sys.exit(0 if result.ok else 1)

    if args.once:
        # ─── 单次冷启动模式：执行一次 force=True 的冷启动并打印结果 ───
        result = scheduler.cold_start_if_needed(client, model=cs_model,
                                                 prompt=cs_prompt, force=True)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # ─── 步骤 5：装配主循环所需的回调（partial 绑定依赖） ───
    # on_cs / on_q 是 Scheduler.tick 的回调签名，副作用（飞书/日志）集中在此
    running = True
    on_cs = partial(_on_cold_start, feishu=feishu, log=log)
    on_q = partial(_on_quota, feishu=feishu, log=log, scheduler=scheduler)

    # ─── --watch 模式：独立循环，不进入守护流程 ───
    if args.watch:
        log.info("进入 --watch 实时仪表盘模式")
        # 简易 watch 循环（每 5 秒更新一次）
        while running:
            try:
                quota = scheduler.check_quota_now(client)
                if quota.get("ok"):
                    log.info("额度: %.1f%% | 重置: %s",
                             quota.get("five_hour_utilization", 0),
                             quota.get("five_hour_resets_at", "N/A"))
                else:
                    log.warning("查询失败: %s", quota.get("error", "未知"))
                time.sleep(5)
            except Exception as e:
                # watch 模式下异常不能让循环死掉，吞掉继续
                log.warning("watch 模式异常: %s", e)
                time.sleep(5)
        return

    # ─── 步骤 6：守护模式——启动飞书监听 + 跑主循环 ───

    # ─── 6.1 启动飞书 Bot 命令监听（前提：配置 enable_bot=True 且 lark-cli 可用） ───
    feishu_bot_active = False
    if f_cfg.get("enable_bot", True) and FeishuBot.check_cli():
        feishu.set_command_handler(handle_command)
        feishu.start_listener()
        feishu_bot_active = True
        log.info("飞书 Bot 命令监听已启动")

    # ─── 6.2 打印启动摘要（便于 journalctl 排查） ───
    log.info("智谱 Coding Plan 管理工具已启动")
    log.info("冷启动时间: %s", ", ".join(scheduler.slots_as_strings()))
    log.info("额度查询间隔: %s 分钟", quota_interval)
    log.info("飞书 Bot: %s", "已启用" if feishu_bot_active else "未启用")
    log.info("日志目录: %s", args.log_dir)
    log.info("日志保留: %s 小时", args.log_retention_hours)

    # ─── 6.3 启动时给告警频道发一条上线通知（如果配置了 chat_id） ───
    if feishu.notify_chat_id:
        feishu.send_message("🚀 智谱 Coding Plan 管理工具已启动")

    # ─── 6.4 启动后立即 tick 一次，不用等到第一个 sleep 结束 ───
    # 这样进程一起来就会查额度/按需冷启动，避免冷启动延迟
    scheduler.tick(client=client, model=cs_model, prompt=cs_prompt,
                   on_cold_start=on_cs, on_quota=on_q)

    # ─── 步骤 7：信号处理函数（闭包，通过 nonlocal 修改外层状态） ───

    def shutdown(_sig, _frame):
        """SIGTERM/SIGINT：翻转 running 标志，让主循环在下次检查时退出。"""
        nonlocal running
        log.info("收到退出信号，正在关闭...")
        running = False

    def reload_config(_sig, _frame):
        """SIGHUP：热重载配置文件中可热更新的字段。

        支持热更新：cold_start_times / quota_check_interval / api_key /
                    cold_start_model / cold_start_prompt
        不支持：provider 切换（需要重启进程，因为 client 类型已固定）

        任何环节失败都只记日志，不抛异常（信号处理函数抛异常会终止进程）。
        """
        nonlocal scheduler, quota_interval, cs_model, cs_prompt, client
        try:
            new_cfg = load_config(args.config)
            new_s_cfg = new_cfg.get("schedule", {})
            new_p_cfg = new_cfg.get(provider, {})
            new_provider = new_cfg.get("provider", "zhipu")

            # ─── 不允许热切换 provider，直接忽略 ───
            if new_provider != provider:
                log.warning("config 热重载不支持切换 provider，忽略")
                return

            # ─── API Key 变了 → 重建 client ───
            new_api_key = new_p_cfg.get("api_key", "")
            if new_api_key and new_api_key != client.api_key:
                try:
                    client = create_client(new_cfg)
                    log.info("API Key 已更新 (热重载)")
                except ValueError as e:
                    log.error("热重载创建 client 失败: %s", e)
                    return

            # ─── 冷启动时间变了 → 更新 scheduler ───
            new_times = new_s_cfg.get("cold_start_times", _DEFAULT_COLD_START_TIMES)
            scheduler.update_cold_start_times(new_times)

            # ─── 额度查询间隔变了 → 更新 scheduler 与本地变量 ───
            new_interval = new_s_cfg.get("quota_check_interval_minutes", _DEFAULT_QUOTA_INTERVAL_MIN)
            scheduler.update_quota_interval(new_interval)
            quota_interval = new_interval

            # ─── 冷启动 model/prompt 变了 → 同步给 router ───
            cs_model_new = new_p_cfg.get("cold_start_model", _DEFAULT_COLD_START_MODEL)
            cs_prompt_new = new_p_cfg.get("cold_start_prompt", _DEFAULT_COLD_START_PROMPT)
            cs_model = cs_model_new
            cs_prompt = cs_prompt_new
            router.update_cold_start_defaults(cs_model, cs_prompt)

            log.info("配置已热重载 (冷启动: %s, 间隔: %smin)",
                     ", ".join(new_times), new_interval)
        except Exception:
            # 兜底：信号回调里绝不能让异常逃出去
            log.exception("热重载配置失败")

    # ─── 7.1 注册信号处理器 ───
    # 信号处理：SIGTERM 最坏等待 1s（比原来更可靠）
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        # Windows 没有 SIGHUP，忽略 AttributeError
        signal.signal(signal.SIGHUP, reload_config)
    except AttributeError:
        pass

    # ─── 步骤 8：主循环 ───
    # 把 30 秒拆成 30 次 1 秒 sleep，是为了收到信号后能尽快退出，
    # 而不是硬等满整个 tick 周期。
    _tick_chunks = _TICK_INTERVAL_SEC

    while running:
        for _ in range(_tick_chunks):
            if not running:
                break
            time.sleep(1)

        if not running:
            break

        try:
            # ─── 推进一次调度（内部决定冷启动/查额度/都不做） ───
            scheduler.tick(client=client, model=cs_model, prompt=cs_prompt,
                           on_cold_start=on_cs, on_quota=on_q)
        except Exception:
            # tick 内部异常不能让主循环挂掉，记录后继续
            log.exception("tick() 异常")

    # ─── 步骤 9：优雅关闭飞书监听 + 打印退出日志 ───
    feishu.stop()
    log.info("已停止")


def _on_cold_start(result: dict, feishu: FeishuBot, log: logging.Logger):
    """Scheduler 冷启动完成后的回调：写日志 + 必要时发飞书。

    通过 partial 在 main() 中绑定 feishu/log，Scheduler 只需传 result。

    Args:
        result: Scheduler.cold_start_if_needed 的返回 dict
                （cold_started / reason / quota 三字段）
        feishu: 飞书 Bot 实例
        log: logger 实例
    """
    if result["cold_started"]:
        log.info("冷启动成功")
        # 冷启动成功时附带最新额度状态，便于用户确认
        feishu.send_status(result["quota"], "✅ 冷启动成功")
    else:
        reason = result["reason"]
        log.info("跳过冷启动: %s", reason)
        # 只有"失败"才推送告警，"窗口未过期跳过"不打扰用户
        if "失败" in reason:
            feishu.send_message(f"❌ 冷启动失败: {reason}")


def _on_quota(quota: dict, feishu: FeishuBot, log: logging.Logger, scheduler):
    """Scheduler 查额度完成后的回调：写日志 + 阈值告警 + 到期告警。

    两种告警（都走飞书默认告警频道）：
    - notify_quota：用量百分比超过阈值时触发（防抖，同一百分比只发一次）
    - 即将到期告警：5 小时窗口在 30 分钟内重置时触发（每个窗口只发一次）

    Args:
        quota: Scheduler.check_quota_now 返回的标准化 dict
        feishu: 飞书 Bot 实例
        log: logger 实例
        scheduler: 调度器实例（借用 _last_window_warning 字段做去重）
    """
    if quota.get("ok"):
        pct = quota.get("five_hour_utilization")
        if pct is not None:
            log.info("额度: %.1f%% | 重置: %s", pct, quota.get("five_hour_resets_at", "N/A"))
        # ─── 阈值告警（内部自带防抖） ───
        feishu.notify_quota(quota)
        # ─── 即将到期告警：用 reset 时间做去重键，避免一个窗口重复推送 ───
        if quota.get("five_hour_expiring_soon"):
            reset_at = quota.get("five_hour_resets_at", "")
            if scheduler._last_window_warning != reset_at:
                scheduler._last_window_warning = reset_at
                log.info("⚠️ 窗口即将到期，%s", FeishuBot._to_beijing(reset_at))
                feishu.send_message(f"⏰ 5小时窗口将在 30 分钟内到期\n{FeishuBot._to_beijing(reset_at)} 重置")


if __name__ == "__main__":
    main()
