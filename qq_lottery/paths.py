"""路径解析基准。

所有相对路径都相对于**程序所在目录**解析，而不是当前工作目录，
这样程序文件夹被移动到任何位置、或从任意目录用绝对路径启动都能正常工作。
"""

from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    """程序根目录（本包的上一级）。"""
    return Path(__file__).resolve().parent.parent


def resolve(path: str | Path, root: Path | None = None) -> Path:
    """把相对路径解析成基于程序目录的绝对路径。"""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return (root or project_root()) / p


def display(path: str | Path, root: Path | None = None) -> str:
    """尽量显示成相对于程序目录的短路径，便于界面展示。"""
    base = root or project_root()
    p = resolve(path, base)
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return str(p)


def ensure_sys_path() -> None:
    """允许以脚本方式直接运行 app/webapp 时也能 import qq_lottery。"""
    root = str(project_root())
    if root not in sys.path:
        sys.path.insert(0, root)
