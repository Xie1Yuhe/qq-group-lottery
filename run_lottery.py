#!/usr/bin/env python3
"""统一入口脚本：双击或命令行运行都可。

    python run_lottery.py draw -n 3
    python run_lottery.py stats
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

from qq_lottery.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
