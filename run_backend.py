from __future__ import annotations

import os
import sys
from multiprocessing import set_executable
from pathlib import Path


def _prepend_venv_paths() -> None:
    venv_root_raw = os.environ.get("APP_VENV_ROOT", "").strip()
    if not venv_root_raw:
        return

    venv_root = Path(venv_root_raw)
    candidates = [
        venv_root,
        venv_root / "Lib" / "site-packages",
    ]
    for path in reversed(candidates):
        resolved = str(path.resolve())
        if path.exists() and resolved not in sys.path:
            sys.path.insert(0, resolved)


_prepend_venv_paths()

import uvicorn


def main() -> None:
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))
    reload_enabled = os.environ.get("APP_RELOAD", "1").strip().lower() not in {"0", "false", "no"}

    # Force uvicorn reload subprocesses to reuse the current virtualenv interpreter.
    set_executable(sys.executable)
    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled)


if __name__ == "__main__":
    main()
