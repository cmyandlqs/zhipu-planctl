"""多厂商 Coding Plan API 客户端。

抽象基类 BasePlanClient 定义统一接口：
- query_quota() → QuotaResult
- cold_start(model, prompt) → bool

各厂商适配器继承 BasePlanClient 实现各自 API 差异。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
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


# ─────────────────── 抽象基类 ───────────────────


class BasePlanClient(ABC):
    """Coding Plan 客户端抽象接口。"""

    def __init__(self, api_key: str, base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        ...

    @abstractmethod
    def cold_start(self, model: str, prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        ...


# ─────────────────── 智谱适配器 ───────────────────


class ZhipuPlanClient(BasePlanClient):
    """智谱 Coding Plan API 客户端。"""

    HOST_CN = "https://open.bigmodel.cn"
    HOST_INTL = "https://api.z.ai"

    def __init__(self, api_key: str, base_url: str = HOST_CN):
        super().__init__(api_key, base_url)
        host = self.HOST_CN if "bigmodel.cn" in base_url.lower() else self.HOST_INTL
        self.quota_url = f"{host}/api/monitor/usage/quota/limit"
        self._host = host

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

    def cold_start(self, model: str = "glm-4-air", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        url = f"{self._host}/api/paas/v4/chat/completions"
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
                if resp.status != 200:
                    return False
                resp_body = json.loads(resp.read().decode("utf-8"))
                return bool(resp_body.get("success", True))
        except urllib_error.HTTPError:
            return False
        except urllib_error.URLError:
            return False
        except Exception:
            return False

    def _parse_tiers(self, data: dict) -> list[Tier]:
        five_hour: Optional[tuple] = None
        weekly: Optional[tuple] = None
        unclassified: list[tuple] = []

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

        return [
            Tier(name=name, utilization=slot[1], resets_at=slot[2])
            for name, slot in ((TIER_FIVE_HOUR, five_hour), (TIER_WEEKLY, weekly))
            if slot
        ]

    @staticmethod
    def _ms_to_iso(ms: int) -> Optional[str]:
        try:
            dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
            return dt.isoformat()
        except (OSError, ValueError, OverflowError):
            return None


# ─────────────────── MiniMax 适配器 (TODO) ───────────────────


class MiniMaxPlanClient(BasePlanClient):
    """MiniMax Coding Plan 适配器。

    待实现：MiniMax 的 API 地址、鉴权方式、配额字段与智谱不同，
    需根据 MiniMax 开放平台文档补充 query_quota / cold_start。
    """

    def __init__(self, api_key: str, base_url: str = ""):
        super().__init__(api_key, base_url)

    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        return QuotaResult(
            ok=False,
            error="MiniMax 适配器尚未实现，请关注后续更新",
            credential_valid=False,
        )

    def cold_start(self, model: str = "", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        return False


# ─────────────────── OpenCode Go 适配器 ───────────────────


class OpenCodeGoPlanClient(BasePlanClient):
    """OpenCode Go Coding Plan 适配器。

    OpenCode Go 是一个订阅制服务（$5/首月，$10/月），提供：
    - 5 小时窗口：$12 限额
    - 每周限额：$30
    - 每月限额：$60

    API 兼容 OpenAI 格式，使用 Authorization: Bearer 认证。
    额度查询通过发送极小请求验证 key 有效性，但无公开 API 返回具体用量。
    用量可在 https://opencode.ai/auth 控制台查看。
    """

    CHAT_URL = "https://opencode.ai/zen/go/v1/chat/completions"

    def __init__(self, api_key: str, base_url: str = ""):
        super().__init__(api_key, base_url)
        # 如果传入了自定义 base_url，从中提取 host 用于 chat API
        if base_url:
            host = base_url.rstrip("/")
            if "/chat/completions" in host:
                host = host.rsplit("/chat/completions", 1)[0]
            self.chat_url = f"{host}/chat/completions"
        else:
            self.chat_url = self.CHAT_URL

    def _request(self, model: str, prompt: str,
                 timeout: float) -> Optional[dict]:
        """发送一个 chat completion 请求，失败返回 None。"""
        if not self.api_key:
            return None

        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,
        }).encode("utf-8")

        req = request.Request(self.chat_url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0")

        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                return None
            # 402 = 余额不足或超限
            if e.code == 402:
                return None
            return None
        except urllib_error.URLError:
            return None
        except Exception:
            return None

    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        """查询 Coding Plan 额度。

        发送极小请求验证 API key 有效性。由于无公开用量查询 API，
        通过请求 success 推断 key 有效，真实用量请到控制台查看。
        """
        if not self.api_key:
            return QuotaResult(ok=False, error="API Key 未配置", credential_valid=False)

        data = self._request(
            model="deepseek-v4-flash",
            prompt="hi",
            timeout=timeout,
        )
        if data is None:
            return QuotaResult(
                ok=False,
                error="OpenCode Go API 鉴权失败或不可用，请检查 key",
                credential_valid=False,
            )

        # 成功调用，key 有效
        # cost 字段可能有当前请求费用，但不是总用量
        return QuotaResult(
            ok=True,
            level="go_subscription",
            tiers=[
                Tier(
                    name=TIER_FIVE_HOUR,
                    utilization=0.0,
                    resets_at=None,
                ),
            ],
            queried_at=int(time.time() * 1000),
            credential_valid=True,
        )

    def cold_start(self, model: str = "deepseek-v4-flash", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        """触发冷启动：发送极小 chat completion 重置 5 小时窗口。"""
        if not self.api_key:
            return False

        data = self._request(model=model, prompt=prompt, timeout=timeout)
        return data is not None and "choices" in data


# ─────────────────── 工厂函数 ───────────────────

_PROVIDER_MAP: dict[str, type[BasePlanClient]] = {
    "zhipu": ZhipuPlanClient,
    "minimax": MiniMaxPlanClient,
    "opencode_go": OpenCodeGoPlanClient,
}


def create_client(cfg: dict) -> BasePlanClient:
    """根据配置中的 provider 字段创建对应的客户端实例。

    cfg 格式：
      {
        "provider": "zhipu",       # 必填
        "zhipu": {"api_key": "..."},
        "minimax": {"api_key": "..."},
        "opencode_go": {"api_key": "..."},
      }
    向后兼容：cfg 含 "api_key" 顶层字段时，自动按 zhipu 处理。
    """
    provider = cfg.get("provider", "zhipu")
    p_cfg = cfg.get(provider, {})
    # 兼容旧配置：直接传入 api_key 而非嵌套
    if not p_cfg and cfg.get("api_key"):
        provider = "zhipu"
        p_cfg = cfg

    cls = _PROVIDER_MAP.get(provider)
    if cls is None:
        raise ValueError(f"不支持的 provider: {provider!r}，可选: {', '.join(_PROVIDER_MAP)}")
    return cls(
        api_key=p_cfg.get("api_key", ""),
        base_url=p_cfg.get("base_url", ""),
    )
