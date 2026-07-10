"""定时调度逻辑。

- 在配置的时间点（默认 7/12/17/22 点）触发冷启动；
- 每 N 分钟查一次额度；
- 用 set[(hour, minute)] 去重每日时间槽。

调度器只负责"该做什么"，副作用（发飞书消息、写日志）通过回调由调用方实现。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


class Scheduler:
    """调度器：单实例，每 tick() 一次最多做两件事（冷启动 + 额度）。"""

    def __init__(self, cold_start_times: list[str],
                 quota_check_interval_minutes: int = 5):
        # 预解析并排序时间槽，方便 in 比较；[(7,0), (12,0), (17,0), (22,0)]
        self.cold_start_slots: list[tuple[int, int]] = sorted(
            self._parse_time(t) for t in cold_start_times
        )
        self.quota_check_interval = timedelta(minutes=quota_check_interval_minutes)
        self._last_quota_check: Optional[datetime] = None
        # 今日已经处理过的冷启动时间槽，防止同分钟内多次 tick 重复触发
        self._processed_slots: set[tuple[int, int]] = set()
        # 当前日期（YYYY-MM-DD），用于跨天判定
        self._current_day: str = datetime.now().strftime("%Y-%m-%d")

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
        """
        if not resets_at_iso:
            # 拿不到时间就按"已过期"处理（保守触发冷启动）
            return True
        try:
            return datetime.now(timezone.utc) >= datetime.fromisoformat(resets_at_iso)
        except (ValueError, TypeError):
            return True

    def _reset_daily(self):
        """跨天时清空时间槽集合，避免第二天漏处理或重复触发。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_day != today:
            self._current_day = today
            self._processed_slots.clear()

    def check_quota_now(self, client) -> dict:
        """拉一次额度，转成 cli 回调友好格式的 dict。"""
        result = client.query_quota()
        data: dict = {
            "ok": result.ok,
            "level": result.level,
            "error": result.error,
            "queried_at": datetime.now().isoformat(),
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
                })
        return data

    def cold_start_if_needed(self, client, model: str,
                             prompt: str = "hi",
                             force: bool = False) -> dict:
        """窗口已过期或强制模式就冷启动，否则跳过。

        force=True 时忽略窗口过期检查，直接冷启动（用于定时时间槽）。
        force=False 时仅窗口已过期才冷启动（用于手动查询）。

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

        ok = client.cold_start(model=model, prompt=prompt)
        return {
            "cold_started": ok,
            "reason": "冷启动成功" if ok else "冷启动请求失败",
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

        # 当前分钟是否在冷启动时间槽里、且今天还没处理过
        is_cs_time = now_slot in self.cold_start_slots
        is_cs_pending = is_cs_time and now_slot not in self._processed_slots

        # 是否到了查额度的时间点（首次必查 + 之后每 N 分钟）
        is_quota_time = (
            self._last_quota_check is None
            or (now - self._last_quota_check) >= self.quota_check_interval
        )

        # 分支 1：是冷启动且今天没处理过 → 强制冷启动（不管窗口是否过期）
        if is_cs_pending:
            self._processed_slots.add(now_slot)
            result = self.cold_start_if_needed(client, model=model, prompt=prompt, force=True)
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
