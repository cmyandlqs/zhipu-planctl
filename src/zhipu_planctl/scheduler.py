"""定时调度逻辑。

- 在配置的时间点触发冷启动；
- 每 N 分钟查一次额度；
- 用 set[(hour, minute)] 去重每日时间槽；
- 支持 ±1min 容错防止 NTP 调钟漏触发。

调度器只负责"该做什么"，副作用（发飞书消息、写日志）通过回调由调用方实现。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

_log = logging.getLogger("zhipu-plan.scheduler")

_DEFAULT_RETRY_MAX = 10
_DEFAULT_RETRY_DELAY_SEC = 2
_EXPIRY_WARN_MINUTES = 30


class Scheduler:
    """调度器：单实例，每 tick() 一次最多做两件事（冷启动 + 额度）。"""

    def __init__(self, cold_start_times: list[str],
                 quota_check_interval_minutes: int = 5,
                 retry_max: int = _DEFAULT_RETRY_MAX,
                 retry_delay_sec: int = _DEFAULT_RETRY_DELAY_SEC):
        self.cold_start_slots: list[tuple[int, int]] = sorted(
            self._parse_time(t) for t in cold_start_times
        )
        self.quota_check_interval = timedelta(minutes=quota_check_interval_minutes)
        self.retry_max = retry_max
        self.retry_delay_sec = retry_delay_sec
        self._last_quota_check: Optional[datetime] = None
        self._processed_slots: set[tuple[int, int]] = set()
        self._current_day: str = datetime.now().strftime("%Y-%m-%d")
        self._last_window_warning: Optional[str] = None

    def update_cold_start_times(self, times: list[str]):
        self.cold_start_slots = sorted(self._parse_time(t) for t in times)

    def update_quota_interval(self, minutes: int):
        self.quota_check_interval = timedelta(minutes=minutes)

    def slots_as_strings(self) -> list[str]:
        return [f"{h:02d}:{m:02d}" for h, m in self.cold_start_slots]

    @staticmethod
    def _parse_time(t: str) -> tuple[int, int]:
        """解析 "HH:MM" → (hour, minute)。格式不对直接抛，避免配置错误被静默吞掉。"""
        parts = t.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"时间格式应为 HH:MM，收到: {t!r}")
        return int(parts[0]), int(parts[1])

    def _is_window_expired(self, resets_at_iso: Optional[str]) -> bool:
        """判断 5 小时窗口是否已经过了重置时间。

        注：API 返回的 resets_at 是带 tz 的 UTC ISO（"2025-01-01T12:00:00+00:00"），
        必须用 tz-aware 的 datetime 来对比，否则 TypeError（修复见 C1）。
        Python < 3.11 的 fromisoformat 不认 Z 后缀，统一替换为 +00:00。
        """
        if not resets_at_iso:
            # 拿不到时间就按"已过期"处理（保守触发冷启动）
            return True
        try:
            if resets_at_iso.endswith("Z"):
                resets_at_iso = resets_at_iso[:-1] + "+00:00"
            return datetime.now(timezone.utc) >= datetime.fromisoformat(resets_at_iso)
        except (ValueError, TypeError):
            return True

    def _reset_daily(self):
        """跨天时清空时间槽集合，避免第二天漏处理或重复触发。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_day != today:
            self._current_day = today
            self._processed_slots.clear()
            self._last_window_warning = None

    def _find_matching_slot(self, now_slot: tuple[int, int]) -> Optional[tuple[int, int]]:
        """±1min 容错匹配冷启动时间槽，防止 NTP 调钟跳过整分钟。"""
        now_minutes = now_slot[0] * 60 + now_slot[1]
        for slot in self.cold_start_slots:
            slot_minutes = slot[0] * 60 + slot[1]
            if abs(now_minutes - slot_minutes) <= 1:
                return slot
        return None

    def _is_window_expiring_soon(self, resets_at_iso: Optional[str]) -> bool:
        """窗口距离重置不足 _EXPIRY_WARN_MINUTES 分钟。"""
        if not resets_at_iso:
            return False
        try:
            t = resets_at_iso
            if t.endswith("Z"):
                t = t[:-1] + "+00:00"
            reset_dt = datetime.fromisoformat(t)
            return timedelta(0) <= (reset_dt - datetime.now(timezone.utc)) <= timedelta(minutes=_EXPIRY_WARN_MINUTES)
        except (ValueError, TypeError):
            return False

    def check_quota_now(self, client) -> dict:
        """拉一次额度，转成 cli 回调友好格式的 dict。"""
        result = client.query_quota()
        data: dict = {
            "ok": result.ok,
            "level": result.level,
            "error": result.error,
            "queried_at": datetime.now(timezone.utc).isoformat(),
        }
        if result.ok:
            five_hour = next(
                (t for t in result.tiers if t.name == "five_hour"),
                None,
            )
            if five_hour:
                data.update({
                    "five_hour_utilization": five_hour.utilization,
                    "five_hour_resets_at": five_hour.resets_at,
                    "five_hour_expired": self._is_window_expired(five_hour.resets_at),
                    "five_hour_expiring_soon": self._is_window_expiring_soon(five_hour.resets_at),
                })
                if not five_hour.resets_at:
                    _log.warning("API 未返回 nextResetTime，重置时间未知")
        return data

    def cold_start_if_needed(self, client, model: str,
                             prompt: str = "hi",
                             force: bool = False) -> dict:
        """窗口已过期或强制模式就冷启动，否则跳过。

        force=True 时忽略窗口过期检查，带重试 + 验证（用于定时时间槽）。
        force=False 时仅窗口已过期才冷启动，不重试（用于手动查询）。

        验证逻辑：冷启动后查 quota，确认 five_hour_expired == False 才算成功。
        返回一个 dict：cold_started(bool), reason(str), quota(dict)。
        """
        quota = self.check_quota_now(client)
        if not quota["ok"]:
            return {"cold_started": False,
                    "reason": f"查询额度失败: {quota.get('error')}",
                    "quota": quota}

        if not force and not quota.get("five_hour_expired", True):
            return {"cold_started": False,
                    "reason": "当前窗口仍在有效期内，跳过冷启动",
                    "quota": quota}

        max_retries = self.retry_max if force else 1

        for attempt in range(1, max_retries + 1):
            cold_ok = client.cold_start(model=model, prompt=prompt)
            if cold_ok:
                time.sleep(self.retry_delay_sec)
                quota = self.check_quota_now(client)
                if quota.get("five_hour_expired") is False:
                    return {
                        "cold_started": True,
                        "reason": f"冷启动成功 (第 {attempt} 次)",
                        "quota": quota,
                    }

            if not cold_ok and force and attempt < max_retries:
                time.sleep(self.retry_delay_sec)

        return {
            "cold_started": False,
            "reason": f"冷启动未确认 (已重试 {max_retries} 次)",
            "quota": quota,
        }

    def tick(self, client, model: str, prompt: str,
             on_cold_start: Callable, on_quota: Callable) -> None:
        """单次推进：根据当前时间决定触发冷启动 / 查额度 / 都触发 / 都不触发。

        设计：tick 一次最多做两件事（当冷启动时间点恰好撞到 quota 间隔时同时跑）。
        """
        self._reset_daily()
        now = datetime.now()
        now_slot = (now.hour, now.minute)

        matched_slot = self._find_matching_slot(now_slot)
        is_cs_pending = matched_slot is not None and matched_slot not in self._processed_slots

        is_quota_time = (
            self._last_quota_check is None
            or (now - self._last_quota_check) >= self.quota_check_interval
        )

        if is_cs_pending:
            result = self.cold_start_if_needed(client, model=model, prompt=prompt, force=True)
            if result["cold_started"]:
                self._processed_slots.add(matched_slot)
            on_cold_start(result)
            # 冷启动内部已经查过额度了（cold_start_if_needed → check_quota_now）；
            # 若也到了 quota 时间，复用这次结果而不是再打一次 API。
            if is_quota_time:
                self._last_quota_check = now
                on_quota(result["quota"])
            return

        # 分支 2：纯 quota 时间点 → 查额度
        if is_quota_time:
            self._last_quota_check = now
            quota = self.check_quota_now(client)
            on_quota(quota)
            return

        # 分支 3：都不是 → 这一 tick 啥也不做
