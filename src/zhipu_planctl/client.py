"""智谱 Coding Plan API 客户端。

封装两件事：
1. 查询当前套餐额度（5 小时窗口 / 周配额）；
2. 触发一次轻量 chat completion 来"冷启动" 5 小时窗口。

实现零外部依赖（仅用 Python 标准库 urllib）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib import request, error as urllib_error


# 套餐层级常量，对应 query_quota 响应里的 tier 标识
TIER_FIVE_HOUR = "five_hour"        # 5 小时窗口
TIER_WEEKLY = "weekly_limit"        # 周配额


@dataclass
class Tier:
    """套餐中的一个限额层级。"""
    name: str                       # "five_hour" / "weekly_limit"
    utilization: float              # 已用百分比 (0-100)
    resets_at: Optional[str] = None # ISO 时间字符串，含时区


@dataclass
class QuotaResult:
    """query_quota 的返回封装。"""
    ok: bool = False               # 整体是否成功
    level: Optional[str] = None    # 套餐等级（来自响应 data.level）
    tiers: list[Tier] = field(default_factory=list)
    error: Optional[str] = None    # 失败时的错误描述
    queried_at: Optional[int] = None   # 毫秒时间戳
    credential_valid: bool = True      # 鉴权是否仍有效


class ZhipuClient:
    """智谱 Coding Plan API 客户端。

    用法：
        client = ZhipuClient(api_key="xxx")
        result = client.query_quota()
        for t in result.tiers:
            ...
        client.cold_start(model="glm-4-air", prompt="hi")
    """

    # 不同地域对应不同 host：bigmodel.cn 走国内，其余走国际站
    HOST_CN = "https://open.bigmodel.cn"
    HOST_INTL = "https://api.z.ai"

    def __init__(self, api_key: str, base_url: str = HOST_CN):
        self.api_key = api_key
        # 根据用户配置的 base_url 自动选 host
        host = self.HOST_CN if "bigmodel.cn" in base_url.lower() else self.HOST_INTL
        self.quota_url = f"{host}/api/monitor/usage/quota/limit"

    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        """查询当前套餐额度。失败时返回 ok=False 的 QuotaResult，不抛异常。"""
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

        # 业务层 success 标志位
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

    def cold_start(self, model: str = "glm-4-air", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        """触发一次极小的 chat completion，让 5 小时窗口重置计时。

        返回 True 的条件：HTTP 2xx 且业务 success=true。
        """
        host = self.quota_url.rsplit("/api", 1)[0]
        url = f"{host}/api/paas/v4/chat/completions"

        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,        # 最便宜的请求
        }).encode("utf-8")

        req = request.Request(url, data=body, method="POST")
        # 注：智谱在 changelog 中是否要求 Bearer 前缀历史上变过；
        # 当前代码直接传 api_key，已知能跑通，未加 "Bearer " 前缀。
        req.add_header("Authorization", self.api_key)
        req.add_header("Content-Type", "application/json")

        try:
            with request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return False
                # 即使 HTTP 200，业务层 success=false 也不算冷启动成功
                resp_body = json.loads(resp.read().decode("utf-8"))
                return bool(resp_body.get("success", True))
        except urllib_error.HTTPError:
            return False
        except urllib_error.URLError:
            return False
        except Exception:
            return False

    def _parse_tiers(self, data: dict) -> list[Tier]:
        """解析响应里的 limits 列表，按 unit 字段归类到 five_hour / weekly_limit。"""
        five_hour: Optional[tuple] = None
        weekly: Optional[tuple] = None
        unclassified: list[tuple] = []

        limits = data.get("limits")
        if not isinstance(limits, list):
            limits = []

        for item in limits:
            if not isinstance(item, dict):
                continue
            # 只关心 token 类型的限额（type == "TOKENS_LIMIT"）
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

            # 智谱用 unit 字段标识限额类型：
            # unit=3 → 5 小时窗口；unit=6 → 周配额；其余视为待分类。
            unit = item.get("unit")
            if unit == 3:
                if five_hour is None:
                    five_hour = entry
            elif unit == 6:
                if weekly is None:
                    weekly = entry
            else:
                unclassified.append(entry)

        # 没匹配 unit 字段的，按 nextResetTime 远近补到 five_hour / weekly（兜底）
        unclassified.sort(key=lambda e: (e[0] is not None, e[0] if e[0] is not None else 0))
        for entry in unclassified:
            if five_hour is None:
                five_hour = entry
            elif weekly is None:
                weekly = entry

        return [
            Tier(name=name, utilization=slot[1], resets_at=slot[2])
            for name, slot in ((TIER_FIVE_HOUR, five_hour), (TIER_WEEKLY, weekly))
            if slot
        ]

    @staticmethod
    def _ms_to_iso(ms: int) -> Optional[str]:
        """把毫秒时间戳转成 ISO 字符串（带 UTC tz，便于跨时区比对）。"""
        try:
            dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
            return dt.isoformat()
        except (OSError, ValueError, OverflowError):
            return None
