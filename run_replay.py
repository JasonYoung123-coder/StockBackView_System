"""实盘历史回放模拟器 CLI 入口。

Usage:
    python run_replay.py --strategy Jason_selector_strategy2.0.3 \
        --start 2025-01-01 --end 2025-06-30 --capital 100000 -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _prepend_venv_paths() -> None:
    venv_root_raw = os.environ.get("APP_VENV_ROOT", "").strip()
    if not venv_root_raw:
        return
    venv_root = Path(venv_root_raw)
    candidates = [venv_root, venv_root / "Lib" / "site-packages"]
    for path in reversed(candidates):
        resolved = str(path.resolve())
        if path.exists() and resolved not in sys.path:
            sys.path.insert(0, resolved)


_prepend_venv_paths()

from app.trading.replay import cli_main

if __name__ == "__main__":
    cli_main()
