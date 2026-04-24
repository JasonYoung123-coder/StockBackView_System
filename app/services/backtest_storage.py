from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_RESULTS_SUBDIR = "backtest_results"


def _results_dir() -> Path:
    d = get_settings().data_dir / _RESULTS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_name(name: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^\w一-鿿\-]", "_", name)
    return cleaned[:max_len]


class _Index:
    def __init__(self) -> None:
        self._map: dict[str, Path] = {}
        self._lock = Lock()
        self._built = False

    def _build(self) -> None:
        if self._built:
            return
        d = _results_dir()
        for fp in d.glob("*.json"):
            try:
                with fp.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                rid = raw.get("record_id")
                if rid:
                    self._map[rid] = fp
            except Exception:
                logger.warning("跳过损坏的回测记录文件: %s", fp.name, exc_info=True)
        self._built = True

    def ensure(self) -> None:
        with self._lock:
            self._build()

    def add(self, record_id: str, path: Path) -> None:
        with self._lock:
            self._map[record_id] = path

    def remove(self, record_id: str) -> Path | None:
        with self._lock:
            return self._map.pop(record_id, None)

    def get(self, record_id: str) -> Path | None:
        with self._lock:
            self._build()
            return self._map.get(record_id)

    def all_paths(self) -> list[tuple[str, Path]]:
        with self._lock:
            self._build()
            return list(self._map.items())


_index = _Index()


def save_result(request_dict: dict[str, Any], result_dict: dict[str, Any]) -> str:
    record_id = str(uuid4())
    saved_at = datetime.now().isoformat(timespec="seconds")

    strategy_name = request_dict.get("strategy_name", "unknown")
    start_date = str(request_dict.get("start_date", "")).replace("-", "")
    end_date = str(request_dict.get("end_date", "")).replace("-", "")
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")

    filename = f"{_sanitize_name(strategy_name)}_{start_date}_{end_date}_{ts}.json"
    filepath = _results_dir() / filename

    payload = {
        "record_id": record_id,
        "saved_at": saved_at,
        "request": request_dict,
        "result": result_dict,
    }

    with filepath.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)

    _index.ensure()
    _index.add(record_id, filepath)
    logger.info("回测结果已保存: %s -> %s", record_id, filepath.name)
    return record_id


def list_records() -> list[dict[str, Any]]:
    _index.ensure()
    summaries: list[dict[str, Any]] = []

    for record_id, fp in _index.all_paths():
        try:
            with fp.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            req = raw.get("request", {})
            res = raw.get("result", {})
            strategy_info = res.get("strategy", {})
            summaries.append({
                "record_id": raw.get("record_id", record_id),
                "saved_at": raw.get("saved_at", ""),
                "strategy_name": strategy_info.get("name", req.get("strategy_name", "")),
                "asset": res.get("asset", ""),
                "start_date": res.get("start_date", req.get("start_date", "")),
                "end_date": res.get("end_date", req.get("end_date", "")),
                "initial_capital": req.get("initial_capital", 0),
                "metrics": res.get("metrics", {}),
                "strategy_params": req.get("strategy_params", {}),
            })
        except Exception:
            logger.warning("读取回测记录摘要失败: %s", fp.name, exc_info=True)

    summaries.sort(key=lambda x: x.get("saved_at", ""), reverse=True)
    return summaries


def get_record(record_id: str) -> dict[str, Any] | None:
    fp = _index.get(record_id)
    if fp is None or not fp.exists():
        return None
    try:
        with fp.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("读取回测记录失败: %s", fp, exc_info=True)
        return None


def delete_record(record_id: str) -> bool:
    fp = _index.remove(record_id)
    if fp is None:
        return False
    try:
        fp.unlink(missing_ok=True)
        logger.info("回测记录已删除: %s", record_id)
        return True
    except Exception:
        logger.warning("删除回测记录文件失败: %s", fp, exc_info=True)
        return False
