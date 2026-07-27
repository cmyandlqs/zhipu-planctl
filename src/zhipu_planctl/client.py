"""
多厂商 Coding Plan API 客户端。

架构设计：适配器模式（Adapter Pattern）
- 抽象基类 BasePlanClient 定义统一接口，所有厂商适配器必须实现
- 每个厂商适配器封装该厂商的 API 差异（URL、鉴权、响应格式）
- 工厂函数 create_client 根据配置动态创建对应适配器实例

统一接口：
- query_quota() → QuotaResult：查询额度，返回标准化结果
- cold_start(model, prompt) → bool：触发冷启动，返回是否成功

设计优势：
- 新增厂商只需继承 BasePlanClient 实现两个方法，无需修改主流程
- 调用方（cli.py、scheduler.py）只依赖抽象接口，与具体厂商解耦
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib import request, error as urllib_error

_log = logging.getLogger("zhipu-plan.client")

# 额度层级名称常量
TIER_FIVE_HOUR = "five_hour"      # 5 小时滚动窗口
TIER_WEEKLY = "weekly_limit"      # 每周限额

# 智谱 API 返回的 unit 字段值（用于识别额度层级）
_ZHIPU_UNIT_FIVE_HOUR = 3          # 5 小时窗口对应的 unit 值
_ZHIPU_UNIT_WEEKLY = 6             # 每周限额对应的 unit 值


@dataclass
class Tier:
    """额度层级数据类。"""
    name: str                        # 层级名称（five_hour 或 weekly_limit）
    utilization: float              # 已用百分比（0-100）
    resets_at: Optional[str] = None  # 重置时间（UTC ISO 格式）


@dataclass
class QuotaResult:
    """额度查询结果数据类。"""
    ok: bool = False                         # 查询是否成功
    level: Optional[str] = None             # 用户等级（智谱返回）
    tiers: list[Tier] = field(default_factory=list)  # 各层级数据
    error: Optional[str] = None              # 错误信息（失败时）
    queried_at: Optional[int] = None         # 查询时间戳（毫秒）
    credential_valid: bool = True           # 凭证是否有效（用于区分鉴权失败和业务错误）


# ─────────────────── 抽象基类 ───────────────────


class BasePlanClient(ABC):
    """Coding Plan 客户端抽象接口。

    定义了所有厂商适配器必须实现的统一接口：
    - query_quota(): 查询额度，返回标准化的 QuotaResult
    - cold_start(): 触发冷启动，返回是否成功

    子类实现示例：
    - ZhipuPlanClient: 智谱 GLM Coding Plan
    - MiniMaxPlanClient: MiniMax（待实现）
    - OpenCodeGoPlanClient: OpenCode Go 订阅服务
    """

    def __init__(self, api_key: str, base_url: str = ""):
        """初始化客户端。

        Args:
            api_key: API 密钥
            base_url: API 基础 URL（可选，子类可自定义）
        """
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        """查询额度。

        Args:
            timeout: 请求超时时间（秒）

        Returns:
            QuotaResult: 标准化的额度查询结果
        """
        ...

    @abstractmethod
    def cold_start(self, model: str, prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        """触发冷启动。

        冷启动是发送一个极小的 API 请求来激活/重置 5 小时使用窗口。
        这是为了确保工作时间窗口内有可用额度。

        Args:
            model: 使用的模型名称
            prompt: 发送的提示内容（通常极短，如 "hi"）
            timeout: 请求超时时间（秒）

        Returns:
            bool: 冷启动是否成功
        """
        ...


# ─────────────────── 智谱适配器 ───────────────────


class ZhipuPlanClient(BasePlanClient):
    """智谱 Coding Plan API 客户端。

    实现了智谱 GLM Coding Plan 的两个接口：
    - 额度查询：GET /api/monitor/usage/quota/limit
    - 冷启动：POST /api/coding/paas/v4/chat/completions（极小请求）

    特点：
    - 支持国内（bigmodel.cn）和国际（api.z.ai）两个 endpoint
    - 解析智谱特有的响应格式（limits 数组、unit 字段）
    - 区分鉴权失败（401/403）和其他错误的响应码
    """

    HOST_CN = "https://open.bigmodel.cn"      # 国内 endpoint
    HOST_INTL = "https://api.z.ai"             # 国际 endpoint

    def __init__(self, api_key: str, base_url: str = HOST_CN):
        """初始化智谱客户端。

        Args:
            api_key: 智谱 API 密钥（格式 id.secret）
            base_url: API 基础 URL（可选，默认 HOST_CN）
        """
        super().__init__(api_key, base_url)
        # 根据 base_url 判断使用国内还是国际 endpoint
        host = self.HOST_CN if "bigmodel.cn" in base_url.lower() else self.HOST_INTL
        self.quota_url = f"{host}/api/monitor/usage/quota/limit"  # 额度查询接口
        self._host = host

    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        """查询智谱 Coding Plan 额度。

        请求流程：
        1. 验证 API Key 是否配置
        2. 构建 GET 请求到额度查询接口
        3. 添加鉴权头（Authorization: id.secret）
        4. 发送请求并解析响应
        5. 调用 _parse_tiers 解析 limits 数组

        Args:
            timeout: 请求超时时间（秒），默认 15 秒

        Returns:
            QuotaResult: 包含 ok、level、tiers（5小时/每周）、error 等字段
        """
        # ─── 步骤 1：API Key 验证 ───
        if not self.api_key:
            return QuotaResult(ok=False, error="API Key 未配置", credential_valid=False)

        # ─── 步骤 2：构建 HTTP 请求 ───
        req = request.Request(self.quota_url, method="GET")
        req.add_header("Authorization", self.api_key)      # 智谱使用完整 API Key 作为鉴权头
        req.add_header("Content-Type", "application/json")

        # ─── 步骤 3：发送请求并处理错误 ───
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as e:
            # 区分鉴权失败（401/403）和其他 HTTP 错误
            if e.code in (401, 403):
                return QuotaResult(ok=False, error=f"鉴权失败 (HTTP {e.code})", credential_valid=False)
            return QuotaResult(ok=False, error=f"API 错误 (HTTP {e.code})")
        except urllib_error.URLError as e:
            return QuotaResult(ok=False, error=f"网络错误: {e.reason}")
        except Exception as e:
            return QuotaResult(ok=False, error=f"请求失败: {e}")

        # ─── 步骤 4：解析响应体 ───
        if not body.get("success", True):
            return QuotaResult(ok=False, error=f"业务错误: {body.get('msg', '未知')}")

        data = body.get("data")
        if not isinstance(data, dict):
            return QuotaResult(ok=False, error="响应缺少 data 字段")

        # ─── 步骤 5：解析额度层级 ───
        tiers = self._parse_tiers(data)       # 提取 5 小时窗口和每周限额
        level = data.get("level") if isinstance(data.get("level"), str) else None

        return QuotaResult(
            ok=True, level=level, tiers=tiers,
            queried_at=int(time.time() * 1000), credential_valid=True,
        )

    def cold_start(self, model: str = "glm-4-air", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        """触发智谱 Coding Plan 冷启动。

        冷启动原理：发送一个极小的 chat completion 请求（max_tokens=1），
        用于激活或重置 5 小时使用窗口。这样在工作时间开始时就有可用额度。

        Args:
            model: 使用的模型名称，默认 glm-4-air（廉价）
            prompt: 发送的提示内容，默认 "hi"（极短，节省 token）
            timeout: 请求超时时间（秒），默认 30 秒

        Returns:
            bool: 冷启动是否成功（响应包含 choices 数组即认为成功）
        """
        # ─── 构建 POST 请求到 chat completions 接口 ───
        url = f"{self._host}/api/coding/paas/v4/chat/completions"
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,  # 只请求 1 个 token，最小化消耗
        }).encode("utf-8")

        req = request.Request(url, data=body, method="POST")
        req.add_header("Authorization", self.api_key)
        req.add_header("Content-Type", "application/json")

        # ─── 发送请求并简化判断成功 ───
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                resp_body = json.loads(resp.read().decode("utf-8"))
                choices = resp_body.get("choices")
                # 只要响应包含 choices 数组就认为成功（不验证具体内容）
                return isinstance(choices, list) and len(choices) > 0
        except urllib_error.HTTPError as e:
            _log.debug("cold_start HTTP %s", e.code)
            return False
        except urllib_error.URLError as e:
            _log.debug("cold_start network error: %s", e.reason)
            return False
        except Exception:
            _log.exception("cold_start unexpected error")
            return False

    def _parse_tiers(self, data: dict) -> list[Tier]:
        """解析智谱 API 响应中的额度层级数据。

        智谱 API 返回的 limits 数组格式：
        [
          {
            "type": "TOKENS_LIMIT",
            "unit": 3,              # 3 = 5小时窗口, 6 = 每周限额
            "percentage": 12.3,     # 已用百分比
            "nextResetTime": 1234567890000  # 重置时间（毫秒时间戳）
          },
          ...
        ]

        解析逻辑：
        1. 遍历 limits 数组，筛选 type="TOKENS_LIMIT" 的项
        2. 根据 unit 字段分类：unit=3 → five_hour, unit=6 → weekly
        3. 其他 unit 的项存入 unclassified，作为后备
        4. 如果某个类别没有匹配项，从 unclassified 中补一个

        Args:
            data: API 响应的 data 字段（dict）

        Returns:
            list[Tier]: 包含 five_hour 和 weekly_limit 两个 Tier 对象
        """
        five_hour: Optional[tuple] = None      # (重置时间戳, 已用百分比, 重置时间ISO)
        weekly: Optional[tuple] = None         # 同上
        unclassified: list[tuple] = []        # 其他未知 unit 的项

        limits = data.get("limits")
        if not isinstance(limits, list):
            limits = []

        for item in limits:
            # ─── 数据验证 ───
            if not isinstance(item, dict):
                continue
            ltype = item.get("type", "")
            if not isinstance(ltype, str) or ltype.upper() != "TOKENS_LIMIT":
                continue

            # ─── 提取百分比和重置时间 ───
            try:
                pct = float(item.get("percentage", 0))
            except (TypeError, ValueError):
                pct = 0.0

            reset_ms = item.get("nextResetTime")
            if not isinstance(reset_ms, int) or isinstance(reset_ms, bool):
                reset_ms = None
            reset_iso = self._ms_to_iso(reset_ms) if reset_ms else None
            entry = (reset_ms, pct, reset_iso)

            # ─── 根据 unit 分类 ───
            unit = item.get("unit")
            if unit == _ZHIPU_UNIT_FIVE_HOUR:
                if five_hour is None:
                    five_hour = entry
            elif unit == _ZHIPU_UNIT_WEEKLY:
                if weekly is None:
                    weekly = entry
            else:
                unclassified.append(entry)

        # ─── 兜底逻辑：用 unclassified 填补缺失的类别 ───
        unclassified.sort(key=lambda e: (e[0] is not None, e[0] if e[0] is not None else 0))
        for entry in unclassified:
            if five_hour is None:
                five_hour = entry
            elif weekly is None:
                weekly = entry

        # ─── 构造 Tier 对象列表 ───
        return [
            Tier(name=name, utilization=slot[1], resets_at=slot[2])
            for name, slot in ((TIER_FIVE_HOUR, five_hour), (TIER_WEEKLY, weekly))
            if slot  # 只返回有数据的类别
        ]

    @staticmethod
    def _ms_to_iso(ms: int) -> Optional[str]:
        """将毫秒时间戳转换为 UTC ISO 字符串。

        Args:
            ms: 毫秒时间戳（如 1234567890000）

        Returns:
            Optional[str]: ISO 格式的时间字符串（如 "2025-01-01T12:00:00+00:00"），
                       无效时间戳返回 None
        """
        if ms <= 0:
            return None
        try:
            dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
            return dt.isoformat()
        except (OSError, ValueError, OverflowError):
            return None


# ─────────────────── MiniMax 适配器 (TODO) ───────────────────


class MiniMaxPlanClient(BasePlanClient):
    """MiniMax Coding Plan 适配器。

    ⚠️ 待实现：MiniMax 的 API 地址、鉴权方式、配额字段与智谱不同。

    TODO：
    - 需根据 MiniMax 开放平台文档实现 query_quota() 方法
    - 需实现 cold_start() 方法
    - 参考智谱适配器的错误处理和响应解析逻辑

    当前返回：固定错误提示，告知用户尚未实现
    """

    def __init__(self, api_key: str, base_url: str = ""):
        super().__init__(api_key, base_url)

    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        """查询 MiniMax 额度（待实现）。"""
        return QuotaResult(
            ok=False,
            error="MiniMax 适配器尚未实现，请关注后续更新",
            credential_valid=False,
        )

    def cold_start(self, model: str = "", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        """触发 MiniMax 冷启动（待实现）。"""
        return False


# ─────────────────── OpenCode Go 适配器 ───────────────────


class OpenCodeGoPlanClient(BasePlanClient):
    """OpenCode Go Coding Plan 适配器。

    OpenCode Go 是一个订阅制服务（$5/首月，$10/月），提供：
    - 5 小时窗口：$12 限额
    - 每周限额：$30
    - 每月限额：$60

    API 特点：
    - 兼容 OpenAI 格式（chat/completions）
    - 使用 Authorization: Bearer 认证（与智谱不同）
    - 无公开额度查询 API，只能通过极小请求验证 key 有效性
    - 具体用量需登录 https://opencode.ai/auth 控制台查看

    实现策略：
    - query_quota(): 发送极小请求验证 key，不返回真实额度数据
    - cold_start(): 发送极小 chat completion 重置窗口
    """

    CHAT_URL = "https://opencode.ai/zen/go/v1/chat/completions"

    def __init__(self, api_key: str, base_url: str = ""):
        """初始化 OpenCode Go 客户端。

        Args:
            api_key: OpenCode Go API 密钥
            base_url: 可选的自定义 chat completions URL（用于测试或私有部署）
        """
        super().__init__(api_key, base_url)
        # ─── 处理自定义 base_url ───
        if base_url:
            host = base_url.rstrip("/")
            if "/chat/completions" in host:
                host = host.rsplit("/chat/completions", 1)[0]
            self.chat_url = f"{host}/chat/completions"
        else:
            self.chat_url = self.CHAT_URL

    def _request(self, model: str, prompt: str,
                 timeout: float) -> Optional[dict]:
        """发送一个 chat completion 请求（内部辅助方法）。

        这是极小请求：max_tokens=1，用于验证 key 或触发冷启动。

        Args:
            model: 模型名称（如 deepseek-v4-flash）
            prompt: 提示内容（如 "hi"）
            timeout: 请求超时时间（秒）

        Returns:
            Optional[dict]: 成功时返回响应 JSON，失败返回 None
        """
        if not self.api_key:
            return None

        # ─── 构建极小请求体 ───
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,  # 只请求 1 个 token
        }).encode("utf-8")

        req = request.Request(self.chat_url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")  # OpenAI 格式：Bearer token
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0")  # 某些 API 需要 User-Agent

        # ─── 发送请求，所有错误都返回 None ───
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):  # 鉴权失败
                return None
            if e.code == 402:  # 余额不足或超限
                return None
            return None
        except urllib_error.URLError:
            return None
        except Exception:
            return None

    def query_quota(self, timeout: float = 15.0) -> QuotaResult:
        """查询 Coding Plan 额度。

        ⚠️ 注意：OpenCode Go 无公开用量查询 API，此方法仅验证 key 有效性。
        真实额度需登录 https://opencode.ai/auth 控制台查看。

        Args:
            timeout: 请求超时时间（秒）

        Returns:
            QuotaResult: key 有效时 ok=True，但 tiers 为空；key 无效时 ok=False
        """
        if not self.api_key:
            return QuotaResult(ok=False, error="API Key 未配置", credential_valid=False)

        # 发送极小请求验证 key
        data = self._request(
            model="deepseek-v4-flash",  # OpenCode Go 推荐的廉价模型
            prompt="hi",
            timeout=timeout,
        )
        if data is None:
            return QuotaResult(
                ok=False,
                error="OpenCode Go API 鉴权失败或不可用，请检查 key",
                credential_valid=False,
            )

        return QuotaResult(
            ok=True,
            level="go_subscription (用量请在 opencode.ai/auth 查看)",
            tiers=[],  # 无公开额度数据，返回空列表
            queried_at=int(time.time() * 1000),
            credential_valid=True,
        )

    def cold_start(self, model: str = "deepseek-v4-flash", prompt: str = "hi",
                   timeout: float = 30.0) -> bool:
        """触发冷启动：发送极小 chat completion 重置 5 小时窗口。

        Args:
            model: 模型名称（默认 deepseek-v4-flash）
            prompt: 提示内容（默认 "hi"）
            timeout: 请求超时时间（秒）

        Returns:
            bool: 冷启动是否成功（响应包含 choices 字段即认为成功）
        """
        if not self.api_key:
            return False

        data = self._request(model=model, prompt=prompt, timeout=timeout)
        return data is not None and "choices" in data


# ─────────────────── 工厂函数 ───────────────────

# 厂商标识符到适配器类的映射表
# 新增厂商时在此注册，create_client 会自动识别
_PROVIDER_MAP: dict[str, type[BasePlanClient]] = {
    "zhipu": ZhipuPlanClient,           # ✅ 已实现
    "minimax": MiniMaxPlanClient,       # ⏳ 待实现
    "opencode_go": OpenCodeGoPlanClient, # ✅ 已实现
}


def create_client(cfg: dict) -> BasePlanClient:
    """根据配置中的 provider 字段创建对应的客户端实例。

    这是适配器模式的工厂方法，根据配置动态创建适配器实例。

    配置格式示例：
      {
        "provider": "zhipu",              # 必填：指定使用哪个厂商
        "zhipu": {"api_key": "sk-xxx"},    # 厂商专属配置
        "minimax": {"api_key": "..."},
        "opencode_go": {"api_key": "..."}
      }

    向后兼容：如果配置文件包含顶层 "api_key" 字段（老版本配置），
    自动按 zhipu 处理。

    Args:
        cfg: 配置字典（从 config.yaml 加载或环境变量构造）

    Returns:
        BasePlanClient: 对应厂商的客户端实例

    Raises:
        ValueError: provider 不支持时抛出，提示可选厂商列表
    """
    # ─── 步骤 1：确定使用哪个厂商 ───
    provider = cfg.get("provider", "zhipu")  # 默认 zhipu
    p_cfg = cfg.get(provider, {})

    # ─── 步骤 2：向后兼容老版本配置 ───
    if provider == "zhipu" and not p_cfg and cfg.get("api_key"):
        p_cfg = cfg  # 使用顶层 api_key 字段

    # ─── 步骤 3：查找适配器类 ───
    cls = _PROVIDER_MAP.get(provider)
    if cls is None:
        # 厂商不支持 → 抛出清晰的错误提示
        supported = ', '.join(_PROVIDER_MAP.keys())
        raise ValueError(f"不支持的 provider: {provider!r}，可选: {supported}")

    # ─── 步骤 4：创建适配器实例 ───
    return cls(
        api_key=p_cfg.get("api_key", ""),      # API Key（必需）
        base_url=p_cfg.get("base_url", ""),    # 自定义 URL（可选）
    )
