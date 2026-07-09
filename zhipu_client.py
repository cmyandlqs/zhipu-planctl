from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib import request, error as urllib_error


TIER_FIVE_HOUR = "five_hour"
TIER_WEEKLY = "weekly_limit"


@dataclass
class Tier:
    name: str
    utilization: float
    resets_at: Optional[str] = None


@dataclass
class QuotaResult:
    ok: bool = False
    level: Optional[str] = None
    tiers: list[Tier] = field(default_factory=list)
    error: Optional[str] = None
    queried_at: Optional[int] = None
    credential_valid: bool = True


class ZhipuClient:

    def __init__(self, api_key: str, base_url: str = "https://open.bigmodel.cn"):
        self.api_key = api_key
        host = "https://open.bigmodel.cn" if "bigmodel.cn" in base_url.lower() else "https://api.z.ai"
        self.quota_url = f"{host}/api/monitor/usage/quota/limit"

    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        if not self.api_key:
            return QuotaResult(ok=False, error="API Key 未配置", credential_valid=False)

        req = request.Request(self.quota_url, method="GET")
        req.add_header("Authorization", self.api_key)
        req.add_header("Content-Type", "application/json")

        try:
            with request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                return QuotaResult(ok=False, error=f"鉴权失败 (HTTP {e.code})", credential_valid=False)
            return QuotaResult(ok=False, error=f"API 错误 (HTTP {e.code})")
        except urllib_error.URLError as e:
            return QuotaResult(ok=False, error=f"网络错误: {e.reason}")
        except Exception as e:
            return QuotaResult(ok=False, error=f"请求失败: {e}")

        if not body.get("success", True):
            return QuotaResult(ok=False, error=f"业务错误: {body.get('msg', '未知')}")

        data = body.get("data")
        if not isinstance(data, dict):
            return QuotaResult(ok=False, error="响应缺少 data 字段")

        tiers = self._parse_tiers(data)
        level = data.get("level") if isinstance(data.get("level"), str) else None

        return QuotaResult(
            ok=True, level=level, tiers=tiers,
            queried_at=int(time.time() * 1000), credential_valid=True,
        )

    def _parse_tiers(self, data: dict) -> list[Tier]:
        five_hour = None
        weekly = None
        unclassified = []

        limits = data.get("limits")
        if not isinstance(limits, list):
            limits = []

        for item in limits:
            if not isinstance(item, dict):
                continue
            ltype = item.get("type", "")
            if not isinstance(ltype, str) or ltype.upper() != "TOKENS_LIMIT":
                continue

            try:
                pct = float(item.get("percentage", 0))
            except (TypeError, ValueError):
                pct = 0.0

            reset_ms = item.get("nextResetTime")
            if not isinstance(reset_ms, int) or isinstance(reset_ms, bool):
                reset_ms = None
            reset_iso = self._ms_to_iso(reset_ms) if reset_ms else None

            entry = (reset_ms, pct, reset_iso)

            unit = item.get("unit")
            if unit == 3:
                if five_hour is None:
                    five_hour = entry
            elif unit == 6:
                if weekly is None:
                    weekly = entry
            else:
                unclassified.append(entry)

        unclassified.sort(key=lambda e: (e[0] is not None, e[0] if e[0] is not None else 0))
        for entry in unclassified:
            if five_hour is None:
                five_hour = entry
            elif weekly is None:
                weekly = entry

        tiers = []
        for name, slot in [(TIER_FIVE_HOUR, five_hour), (TIER_WEEKLY, weekly)]:
            if slot:
                tiers.append(Tier(name=name, utilization=slot[1], resets_at=slot[2]))
        return tiers

    @staticmethod
    def _ms_to_iso(ms: int) -> Optional[str]:
        try:
            dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
            return dt.isoformat()
        except (OSError, ValueError, OverflowError):
            return None

    def cold_start(self, model: str = "glm-4-air", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        host = self.quota_url.rsplit("/api", 1)[0]
        url = f"{host}/api/paas/v4/chat/completions"

        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,
        }).encode("utf-8")

        req = request.Request(url, data=body, method="POST")
        req.add_header("Authorization", self.api_key)
        req.add_header("Content-Type", "application/json")

        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except urllib_error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            return False
        except urllib_error.URLError as e:
            return False
        except Exception as e:
            return False
