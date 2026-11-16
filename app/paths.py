"""Project paths. Relative paths stay inside this app; absolute paths are user output folders."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def data_home() -> Path:
    raw = os.environ.get("MP_CONTENT_HOME")
    path = Path(raw) if raw else PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolve_path(value: str) -> Path:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("路径是空的")
    path = Path(raw)
    if path.is_absolute():
        return path
    norm = raw.replace("\\", "/").lstrip("/")
    if norm.startswith("resources/"):
        base = PROJECT_ROOT.resolve()
    else:
        base = data_home()
    resolved = (base / norm).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("相对路径不能越出项目数据目录")
    return resolved
