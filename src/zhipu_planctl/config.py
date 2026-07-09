"""配置加载逻辑。"""

from __future__ import annotations

import os

import yaml

CONFIG_PATHS = [
    "./config.yaml",
    os.path.expanduser("~/.config/zhipu-plan/config.yaml"),
    os.path.expanduser("~/.zhipu-plan.yaml"),
]


def load_config(path: str | None = None) -> dict:
    """从指定路径或默认候选路径加载 YAML 配置。"""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    for cp in CONFIG_PATHS:
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                return yaml.safe_load(f)

    raise FileNotFoundError(
        "未找到配置文件。请创建 config.yaml，参考 config.yaml.example"
    )
