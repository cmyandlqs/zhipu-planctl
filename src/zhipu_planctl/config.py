"""
配置加载逻辑。

职责：从 YAML 文件或环境变量加载项目配置，支持环境变量覆盖敏感字段。

设计目标：
- 优先使用配置文件（./config.yaml 或 ~/.config/zhipu-plan/config.yaml）
- 环境变量可覆盖 API Key 等敏感字段，方便 CI/CD
- 配置不存在时提供清晰的错误提示和解决方案
"""

from __future__ import annotations

import os
from typing import Optional

import yaml


# 配置文件搜索路径（按优先级排序）
# 1. 当前目录的 config.yaml（优先，适合本地开发）
# 2. 用户配置目录（适合全局安装，多项目共享）
CONFIG_PATHS = ["./config.yaml", os.path.expanduser("~/.config/zhipu-plan/config.yaml")]


def load_config(path: Optional[str] = None) -> dict:
    """从指定路径或默认候选路径加载 YAML 配置。

    加载流程：
    1. 优先使用指定的路径（path 参数）
    2. 否则按 CONFIG_PATHS 顺序搜索默认位置
    3. 环境变量可覆盖敏感字段（API Key、模型、提示词）

    Args:
        path: 可选的配置文件路径，为 None 时搜索默认位置

    Returns:
        dict: 加载后的配置字典，结构示例：
            {
                "provider": "zhipu",
                "zhipu": {"api_key": "sk-xxx", "cold_start_model": "glm-4.7", ...},
                "schedule": {"cold_start_times": [...], "quota_check_interval_minutes": 5},
                "feishu": {"notify_chat_id": "...", ...}
            }

    Raises:
        FileNotFoundError: 配置文件不存在且无环境变量覆盖时
    """
    # ─── 步骤 1：加载 YAML 配置文件 ───
    if path and os.path.exists(path):
        # 用户指定了路径 → 直接读取
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        # 用户没指定 → 按默认候选路径搜索
        cfg = {}
        for cp in CONFIG_PATHS:
            if os.path.exists(cp):
                with open(cp, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                break  # 找到第一个就停止

    # ─── 步骤 2：环境变量覆盖（优先级高于文件） ───
    # 确定使用的厂商：环境变量 PROVIDER > 配置文件 provider > 默认 "zhipu"
    provider = os.environ.get("PROVIDER", cfg.get("provider", "zhipu")).lower()

    # 构造环境变量键名：ZHIPU_API_KEY、OPENCODE_GO_API_KEY 等
    env_key = f"{provider.upper()}_API_KEY"

    if env_key in os.environ:
        # 环境变量存在 → 覆盖配置文件中的 API Key
        cfg.setdefault("provider", provider)
        cfg["provider"] = provider
        p_cfg = cfg.setdefault(provider, {})
        p_cfg["api_key"] = os.environ[env_key]

        # 同时支持覆盖其他字段（可选）
        if "COLD_START_MODEL" in os.environ:
            p_cfg["cold_start_model"] = os.environ["COLD_START_MODEL"]
        if "COLD_START_PROMPT" in os.environ:
            p_cfg["cold_start_prompt"] = os.environ["COLD_START_PROMPT"]

        return cfg  # 环境变量完整覆盖 → 直接返回

    # ─── 步骤 3：配置完整性检查 ───
    if cfg:
        return cfg  # 配置文件存在 → 返回

    # 既没有配置文件，也没有环境变量 → 抛出友好的错误提示
    raise FileNotFoundError(
        f"未找到配置文件，已搜索: {', '.join(CONFIG_PATHS)}\n"
        "请创建 config.yaml（参考 config.yaml.example），\n"
        "或者通过环境变量设置 API Key，例如：ZHIPU_API_KEY=sk-xxx python -m zhipu_planctl"
    )
