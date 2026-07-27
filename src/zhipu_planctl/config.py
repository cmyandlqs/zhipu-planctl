"""配置加载逻辑。"""

from __future__ import annotations

import os
from typing import Optional

import yaml


CONFIG_PATHS = ["./config.yaml", os.path.expanduser("~/.config/zhipu-plan/config.yaml")]


def load_config(path: Optional[str] = None) -> dict:
    """从指定路径或默认候选路径加载 YAML 配置。

    优先从环境变量读取 API Key（ZHIPU_API_KEY 等）。
    """
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}
        for cp in CONFIG_PATHS:
            if os.path.exists(cp):
                with open(cp, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                break

    provider = os.environ.get("PROVIDER", cfg.get("provider", "zhipu")).lower()
    env_key = f"{provider.upper()}_API_KEY"
    if env_key in os.environ:
        cfg.setdefault("provider", provider)
        cfg["provider"] = provider
        p_cfg = cfg.setdefault(provider, {})
        p_cfg["api_key"] = os.environ[env_key]
        if "COLD_START_MODEL" in os.environ:
            p_cfg["cold_start_model"] = os.environ["COLD_START_MODEL"]
        if "COLD_START_PROMPT" in os.environ:
            p_cfg["cold_start_prompt"] = os.environ["COLD_START_PROMPT"]
        return cfg

    if cfg:
        return cfg

    raise FileNotFoundError(
        f"未找到配置文件，已搜索: {', '.join(CONFIG_PATHS)}\n"
        "请创建 config.yaml（参考 config.yaml.example），\n"
        "或者通过环境变量设置 API Key，例如：ZHIPU_API_KEY=sk-xxx python -m zhipu_planctl"
    )
