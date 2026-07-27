"""
定时调度逻辑。

核心职责：
- 在配置的时间点（如 06:00、11:00）触发冷启动
- 每隔 N 分钟查询一次额度，监控使用情况
- 判断 5 小时窗口是否过期，决定是否需要冷启动

设计特点：
- 使用 set[(hour, minute)] 存储每日时间槽，自动去重
- 支持 ±1 分钟容错，防止 NTP 调钟跳过整分钟导致漏触发
- 跨天时自动重置已处理时间槽集合
- 冷启动带重试机制（最多 10 次）+ 验证逻辑（确认窗口已刷新）

副作用分离：
- 调度器只负责"该做什么"，不直接发飞书消息或写日志
- 通过回调函数 on_cold_start / on_quota 把副作用交给调用方（cli.py）处理
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

_log = logging.getLogger("zhipu-plan.scheduler")

# 默认常量
_DEFAULT_RETRY_MAX = 10           # 冷启动最大重试次数
_DEFAULT_RETRY_DELAY_SEC = 2     # 重试间隔（秒）
_EXPIRY_WARN_MINUTES = 30         # 窗口到期前多少分钟发告警


class Scheduler:
    """调度器：单实例，每 tick() 一次最多做两件事（冷启动 + 额度）。

    核心设计：
    - 每次调用 tick() 推进时间，根据当前时刻决定该做什么
    - 用 set[(hour, minute)] 记录今天已处理的时间槽，防止重复触发
    - 跨天时自动重置，避免第二天漏处理或重复触发

    使用示例：
        scheduler = Scheduler(
            cold_start_times=["06:00", "11:00", "16:00", "21:00"],
            quota_check_interval_minutes=5
        )
        while True:
            scheduler.tick(client, model, prompt,
                         on_cold_start=handle_cs,
                         on_quota=handle_quota)
            time.sleep(30)  # 每 30 秒 tick 一次
    """

    def __init__(self, cold_start_times: list[str],
                 quota_check_interval_minutes: int = 5,
                 retry_max: int = _DEFAULT_RETRY_MAX,
                 retry_delay_sec: int = _DEFAULT_RETRY_DELAY_SEC):
        """初始化调度器。

        Args:
            cold_start_times: 冷启动时间点列表，如 ["06:00", "11:00", "16:00", "21:00"]
            quota_check_interval_minutes: 额度查询间隔（分钟），默认 5 分钟
            retry_max: 冷启动失败时最大重试次数，默认 10 次
            retry_delay_sec: 重试间隔（秒），默认 2 秒
        """
        # ─── 解析并排序冷启动时间槽 ───
        self.cold_start_slots: list[tuple[int, int]] = sorted(
            self._parse_time(t) for t in cold_start_times
        )

        # ─── 初始化调度参数 ───
        self.quota_check_interval = timedelta(minutes=quota_check_interval_minutes)
        self.retry_max = retry_max
        self.retry_delay_sec = retry_delay_sec

        # ─── 状态管理字段 ───
        self._last_quota_check: Optional[datetime] = None    # 上次查额度的时间
        self._processed_slots: set[tuple[int, int]] = set()   # 今天已处理的冷启动时间槽
        self._current_day: str = datetime.now().strftime("%Y-%m-%d")  # 当前日期（用于跨天检测）
        self._last_window_warning: Optional[str] = None         # 上次告警的重置时间（防止重复告警）

    def update_cold_start_times(self, times: list[str]):
        """更新冷启动时间点（支持 SIGHUP 热重载）。

        Args:
            times: 新的时间点列表，如 ["07:00", "12:00", "17:00", "22:00"]
        """
        self.cold_start_slots = sorted(self._parse_time(t) for t in times)

    def update_quota_interval(self, minutes: int):
        """更新额度查询间隔（支持 SIGHUP 热重载）。

        Args:
            minutes: 新的查询间隔（分钟）
        """
        self.quota_check_interval = timedelta(minutes=minutes)

    def slots_as_strings(self) -> list[str]:
        """将冷启动时间槽格式化为字符串列表（用于日志和飞书消息）。

        Returns:
            list[str]: 格式化的时间列表，如 ["06:00", "11:00", "16:00", "21:00"]
        """
        return [f"{h:02d}:{m:02d}" for h, m in self.cold_start_slots]

    @staticmethod
    def _parse_time(t: str) -> tuple[int, int]:
        """解析 "HH:MM" 格式的时间字符串为 (hour, minute) 元组。

        设计考虑：
        - 格式不对直接抛 ValueError，避免配置错误被静默吞掉
        - 范围检查：00:00-23:59，防止无效时间

        Args:
            t: 时间字符串，如 "06:30"、"23:59"

        Returns:
            tuple[int, int]: (hour, minute)，如 (6, 30)

        Raises:
            ValueError: 格式或范围不正确时
        """
        parts = t.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"时间格式应为 HH:MM，收到: {t!r}")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError as exc:
            raise ValueError(f"时间格式应为 HH:MM，收到: {t!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间超出范围，应为 00:00-23:59，收到: {t!r}")
        return hour, minute

    def _is_window_expired(self, resets_at_iso: Optional[str]) -> bool:
        """判断 5 小时窗口是否已经过了重置时间。

        设计考虑：
        - 如果拿不到重置时间，保守地按"已过期"处理（触发冷启动）
        - Python < 3.11 的 fromisoformat 不认 "Z" 后缀，统一替换为 "+00:00"
        - 必须使用 tz-aware datetime 对比，否则 TypeError

        Args:
            resets_at_iso: UTC ISO 格式的重置时间，如 "2025-01-01T12:00:00+00:00"

        Returns:
            bool: True 表示窗口已过期，需要冷启动
        """
        if not resets_at_iso:
            # 拿不到时间就按"已过期"处理（保守触发冷启动）
            return True
        try:
            # ─── 兼容 Python < 3.11：将 "Z" 后缀替换为 "+00:00" ───
            if resets_at_iso.endswith("Z"):
                resets_at_iso = resets_at_iso[:-1] + "+00:00"
            # ─── 用 tz-aware datetime 对比 ───
            return datetime.now(timezone.utc) >= datetime.fromisoformat(resets_at_iso)
        except (ValueError, TypeError):
            return True

    def _reset_daily(self):
        """跨天时清空时间槽集合，避免第二天漏处理或重复触发。

        工作机制：
        - 每次调用检查当前日期是否变化
        - 变化时清空已处理时间槽集合和告警状态
        - 这样第二天可以从头开始，不会累积第一天已处理的时间槽
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_day != today:
            self._current_day = today
            self._processed_slots.clear()
            self._last_window_warning = None

    def _find_matching_slot(self, now_slot: tuple[int, int]) -> Optional[tuple[int, int]]:
        """±1min 容错匹配冷启动时间槽，防止 NTP 调钟跳过整分钟。

        设计原理：
        - 如果配置的冷启动时间是 06:00，但 NTP 调钟后当前是 06:01
        - 仍然认为匹配成功，避免因为时钟跳过整分钟而漏触发
        - 这对于守护进程的长期运行很重要

        Args:
            now_slot: 当前时间 (hour, minute)，如 (6, 1)

        Returns:
            Optional[tuple[int, int]]: 匹配到的时间槽，如 (6, 0)；不匹配则返回 None
        """
        now_minutes = now_slot[0] * 60 + now_slot[1]
        for slot in self.cold_start_slots:
            slot_minutes = slot[0] * 60 + slot[1]
            # ±1 分钟容错：abs(now - slot) <= 1
            if abs(now_minutes - slot_minutes) <= 1:
                return slot
        return None

    def _is_window_expiring_soon(self, resets_at_iso: Optional[str]) -> bool:
        """判断窗口是否即将到期（30 分钟内）。

        用于提前告警，让用户知道窗口快要重置了。

        Args:
            resets_at_iso: UTC ISO 格式的重置时间

        Returns:
            bool: True 表示窗口将在 30 分钟内到期
        """
        if not resets_at_iso:
            return False
        try:
            # ─── 兼容 "Z" 后缀 ───
            t = resets_at_iso
            if t.endswith("Z"):
                t = t[:-1] + "+00:00"
            reset_dt = datetime.fromisoformat(t)
            # ─── 判断：重置时间 - 当前时间 <= 30 分钟 ───
            return timedelta(0) <= (reset_dt - datetime.now(timezone.utc)) <= timedelta(minutes=_EXPIRY_WARN_MINUTES)
        except (ValueError, TypeError):
            return False

    def check_quota_now(self, client) -> dict:
        """拉一次额度，转成 cli 回调友好格式的 dict。

        这是供 cli 回调使用的公共接口，返回标准化字典格式：
        {
            "ok": True/False,
            "level": "用户等级",
            "error": "错误信息",
            "queried_at": "查询时间（ISO）",
            "five_hour_utilization": 已用百分比,
            "five_hour_resets_at": "重置时间（ISO）",
            "five_hour_expired": True/False,
            "five_hour_expiring_soon": True/False
        }

        Args:
            client: 厂商客户端实例（ZhipuPlanClient / OpenCodeGoPlanClient）

        Returns:
            dict: 标准化的额度查询结果
        """
        # ─── 步骤 1：调用厂商客户端查询额度 ───
        result = client.query_quota()

        # ─── 步骤 2：包装成标准格式 ───
        data: dict = {
            "ok": result.ok,
            "level": result.level,
            "error": result.error,
            "queried_at": datetime.now(timezone.utc).isoformat(),
        }

        # ─── 步骤 3：提取 5 小时窗口的详细信息 ───
        if result.ok:
            # 从 tiers 数组中找到 five_hour 层级
            five_hour = next(
                (t for t in result.tiers if t.name == "five_hour"),
                None,  # 找不到返回 None
            )
            if five_hour:
                data.update({
                    "five_hour_utilization": five_hour.utilization,      # 已用百分比（0-100）
                    "five_hour_resets_at": five_hour.resets_at,          # 重置时间（ISO）
                    "five_hour_expired": self._is_window_expired(five_hour.resets_at),  # 是否已过期
                    "five_hour_expiring_soon": self._is_window_expiring_soon(five_hour.resets_at),  # 是否即将到期
                })
                if not five_hour.resets_at:
                    _log.warning("API 未返回 nextResetTime，重置时间未知")
        return data

    def cold_start_if_needed(self, client, model: str,
                             prompt: str = "hi",
                             force: bool = False) -> dict:
        """窗口已过期或强制模式就冷启动，否则跳过。

        核心逻辑：
        - 先查询一次当前额度状态
        - force=True（定时触发）：带重试 + 验证，最多重试 10 次
        - force=False（手动触发）：仅窗口过期时启动，不重试
        - 验证机制：冷启动后重新查额度，确认窗口已刷新才算成功

        Args:
            client: 厂商客户端实例
            model: 使用的模型名称
            prompt: 冷启动发送的提示内容
            force: 强制模式标志（True=定时触发，False=手动触发）

        Returns:
            dict: 包含三个字段：
                - cold_started (bool): 是否成功冷启动
                - reason (str): 原因说明
                - quota (dict): 最新的额度查询结果
        """
        # ─── 步骤 1：先查当前额度状态 ───
        quota = self.check_quota_now(client)
        if not quota["ok"]:
            return {"cold_started": False,
                    "reason": f"查询额度失败: {quota.get('error')}",
                    "quota": quota}

        # ─── 步骤 2：判断是否需要冷启动 ───
        if not force and not quota.get("five_hour_expired", True):
            return {"cold_started": False,
                    "reason": "当前窗口仍在有效期内，跳过冷启动",
                    "quota": quota}

        # ─── 步骤 3：执行冷启动（带重试机制） ───
        # force=True 时最多重试 10 次，force=False 时只试 1 次
        max_retries = self.retry_max if force else 1

        for attempt in range(1, max_retries + 1):
            # ─── 发送冷启动请求 ───
            cold_ok = client.cold_start(model=model, prompt=prompt)
            if cold_ok:
                # ─── 冷启动成功 → 等待 2 秒让服务器生效 ───
                time.sleep(self.retry_delay_sec)
                # ─── 重新查额度，验证窗口是否真的刷新了 ───
                quota = self.check_quota_now(client)
                if quota.get("five_hour_expired") is False:
                    # ─── 验证通过：窗口确实刷新了 ───
                    return {
                        "cold_started": True,
                        "reason": f"冷启动成功 (第 {attempt} 次)",
                        "quota": quota,
                    }

            # ─── 本次失败且还有重试机会 → 等待后重试 ───
            if not cold_ok and force and attempt < max_retries:
                time.sleep(self.retry_delay_sec)

        # ─── 所有重试都失败 ───
        return {
            "cold_started": False,
            "reason": f"冷启动未确认 (已重试 {max_retries} 次)",
            "quota": quota,
        }

    def tick(self, client, model: str, prompt: str,
             on_cold_start: Callable, on_quota: Callable) -> None:
        """单次推进：根据当前时间决定触发冷启动 / 查额度 / 都触发 / 都不触发。

        这是调度器的核心方法，每次调用推进一次时间，决定该做什么。
        设计目标：tick 一次最多做两件事（当冷启动时间点恰好撞到 quota 间隔时同时跑）。

        调用示例：
            while True:
                scheduler.tick(
                    client=zhipu_client,
                    model="glm-4.7",
                    prompt="hi",
                    on_cold_start=handle_cs_result,   # 回调：处理冷启动结果
                    on_quota=handle_quota_result     # 回调：处理额度查询结果
                )
                time.sleep(30)  # 每 30 秒 tick 一次

        三个分支逻辑：
        1. 冷启动挂起 + 额度时间 → 同时执行（复用额度查询）
        2. 纯冷启动时间 → 只执行冷启动
        3. 纯额度时间 → 只执行额度查询
        4. 都不是 → 什么都不做

        Args:
            client: 厂商客户端实例
            model: 冷启动使用的模型名称
            prompt: 冷启动发送的提示内容
            on_cold_start: 冷启动完成的回调函数（接收 cold_started 结果）
            on_quota: 额度查询完成的回调函数（接收额度数据）
        """
        # ─── 前置检查：跨天重置 ───
        self._reset_daily()

        # ─── 获取当前时间 ───
        now = datetime.now()
        now_slot = (now.hour, now.minute)

        # ─── 判断是否到冷启动时间点（±1min 容错） ───
        matched_slot = self._find_matching_slot(now_slot)
        is_cs_pending = matched_slot is not None and matched_slot not in self._processed_slots

        # ─── 判断是否到查额度时间点（每 N 分钟） ───
        is_quota_time = (
            self._last_quota_check is None                      # 从未查过 → 该查了
            or (now - self._last_quota_check) >= self.quota_check_interval  # 距上次查够久 → 该查了
        )

        # ─── 分支 1：冷启动时间点到了 ───
        if is_cs_pending:
            # 执行冷启动（force=True → 带重试 + 验证）
            result = self.cold_start_if_needed(client, model=model, prompt=prompt, force=True)
            if result["cold_started"]:
                # 标记这个时间槽今天已处理
                self._processed_slots.add(matched_slot)
            # 调用回调：处理冷启动结果（发飞书、写日志等）
            on_cold_start(result)

            # 优化：如果也到了查额度时间，复用冷启动时的额度查询结果
            if is_quota_time:
                self._last_quota_check = now
                on_quota(result["quota"])
            return  # 完成本次 tick

        # ─── 分支 2：纯额度时间点 ───
        if is_quota_time:
            self._last_quota_check = now
            quota = self.check_quota_now(client)
            on_quota(quota)
            return  # 完成本次 tick

        # ─── 分支 3：都不是 → 这一 tick 啥也不做 ───
        # （节省 API 调用，等待下一个时间点）
