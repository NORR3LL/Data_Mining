from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"找不到配置文件：{path}。请复制 config.example.yaml 为 config.yaml 后修改。"
        )
    with path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}

    for key in ("site", "tasks"):
        if key not in config:
            raise ValueError(f"配置文件缺少必填项：{key}")
    return config

