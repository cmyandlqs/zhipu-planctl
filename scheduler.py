from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional, Callable


class Scheduler:

    def __init__(self, cold_start_times: list[str],
                 quota_check_interval_minutes: int = 5):
        self.cold_start_slots = sorted(
            self._parse_time(t) for t in cold_start_times
        )
        self.quota_check_interval = timedelta(minutes=quota_check_interval_minutes)
        self._last_quota_check: Optional[datetime] = None
        self._processed_slots: set[tuple[int, int]] = set()

    @staticmethod
    def _parse_time(t: str) -> tuple[int, int]:
        parts = t.strip().split(":")
        return int(parts[0]), int(parts[1])

    def _is_window_expired(self, resets_at_iso: Optional[str]) -> bool:
        if not resets_at_iso:
            return True
        try:
            return datetime.now() >= datetime.fromisoformat(resets_at_iso)
        except (ValueError, TypeError):
            return True

    def _reset_daily(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if not hasattr(self, "_current_day") or self._current_day != today:
            self._current_day = today
            self._processed_slots.clear()

    def check_quota_now(self, zhipu_client) -> dict:
        result = zhipu_client.query_quota()
        data = {
            "ok": result.ok,
            "level": result.level,
            "error": result.error,
            "queried_at": datetime.now().isoformat(),
        }
        if result.ok:
            five_hour = None
            for t in result.tiers:
                if t.name == "five_hour":
                    five_hour = t
                    break
            if five_hour:
                data.update({
                    "five_hour_utilization": five_hour.utilization,
                    "five_hour_resets_at": five_hour.resets_at,
                    "five_hour_expired": self._is_window_expired(five_hour.resets_at),
                })
        return data

    def cold_start_if_needed(self, zhipu_client, model: str,
                             prompt: str = "hi") -> dict:
        quota = self.check_quota_now(zhipu_client)
        if not quota["ok"]:
            return {"cold_started": False,
                    "reason": f"查询额度失败: {quota.get('error')}",
                    "quota": quota}

        if not quota.get("five_hour_expired", True):
            return {"cold_started": False,
                    "reason": "当前窗口仍在有效期内，跳过冷启动",
                    "quota": quota}

        ok = zhipu_client.cold_start(model=model, prompt=prompt)
        return {
            "cold_started": ok,
            "reason": "冷启动成功" if ok else "冷启动请求失败",
            "quota": quota,
        }

    def tick(self, zhipu_client, model: str, prompt: str,
             on_cold_start: Callable, on_quota: Callable) -> None:
        self._reset_daily()
        now = datetime.now()
        now_slot = (now.hour, now.minute)

        is_cs_time = now_slot in self.cold_start_slots
        is_cs_pending = is_cs_time and now_slot not in self._processed_slots

        is_quota_time = (
            self._last_quota_check is None
            or (now - self._last_quota_check) >= self.quota_check_interval
        )

        if is_cs_pending:
            self._processed_slots.add(now_slot)
            result = self.cold_start_if_needed(zhipu_client, model=model, prompt=prompt)
            on_cold_start(result)
            return

        if is_quota_time:
            self._last_quota_check = now
            quota = self.check_quota_now(zhipu_client)
            on_quota(quota)
            return
