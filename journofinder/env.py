"""极简环境变量加载：进程启动时 load 一次 .env，之后用 get/require 取值。

各 API key 都用 get（非 require），未设时上层自动跳过该能力（与 pulse 一致），
不会拖垮整条流水线。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

_loaded = False


def load() -> None:
    global _loaded
    if not _loaded:
        load_dotenv()  # 读取 cwd 下的 .env
        _loaded = True


def get(key: str, default: str | None = None) -> str | None:
    load()
    return os.getenv(key, default)


def require(key: str) -> str:
    v = get(key)
    if not v:
        raise RuntimeError(f"环境变量 {key} 未设置")
    return v
