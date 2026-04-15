"""通过 Tushare pro.rt_k 接口获取 A 股实时行情数据。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd
import tushare as ts

from app.core.config import require_tushare_token

logger = logging.getLogger(__name__)

_BATCH_PATTERNS = [
    "6*.SH",
    "0*.SZ",
    "3*.SZ",
    "688*.SH",
    "9*.BJ",
]

_pro: ts.pro_api | None = None


def _get_pro():
    global _pro
    if _pro is None:
        _pro = ts.pro_api(require_tushare_token())
    return _pro


def _fetch_batch(pattern: str) -> pd.DataFrame:
    """拉取单个通配符批次的实时行情。"""
    try:
        df = _get_pro().rt_k(ts_code=pattern)
        if df is not None and not df.empty:
            return df
    except Exception as exc:
        logger.warning("rt_k(%s) 失败: %s", pattern, exc)
    return pd.DataFrame()


def fetch_realtime_quotes() -> pd.DataFrame:
    """分批并发拉取全市场实时行情，返回合并后的 DataFrame。

    使用 ThreadPoolExecutor 并发拉取 5 个板块批次（沪主板、深主板、
    创业板、科创板、北交所），显著提升速度。
    """
    dfs: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=len(_BATCH_PATTERNS)) as pool:
        futures = {pool.submit(_fetch_batch, p): p for p in _BATCH_PATTERNS}
        for future in as_completed(futures):
            pattern = futures[future]
            try:
                df = future.result()
                if not df.empty:
                    dfs.append(df)
                    logger.debug("rt_k(%s): %d 条", pattern, len(df))
            except Exception as exc:
                logger.warning("rt_k(%s) 异常: %s", pattern, exc)

    if not dfs:
        return pd.DataFrame()

    merged = pd.concat(dfs, ignore_index=True)
    merged = merged.drop_duplicates(subset=["ts_code"], keep="first")
    return merged


def get_realtime_prices(ts_codes: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """获取实时行情字典，key=ts_code。

    每只股票返回：
        latest_price, open, high, low, close(=close/最新价),
        vol(股), amount(元), pre_close(昨收), name
    """
    df = fetch_realtime_quotes()
    if df.empty:
        return {}

    if ts_codes:
        codes_set = set(ts_codes)
        df = df[df["ts_code"].isin(codes_set)]

    result: dict[str, dict[str, Any]] = {}
    for row in df.itertuples(index=False):
        ts_code = getattr(row, "ts_code", "")
        if not ts_code:
            continue
        close_price = float(getattr(row, "close", 0) or 0)
        result[ts_code] = {
            "name": getattr(row, "name", "") or "",
            "latest_price": close_price,
            "open": float(getattr(row, "open", 0) or 0),
            "high": float(getattr(row, "high", 0) or 0),
            "low": float(getattr(row, "low", 0) or 0),
            "close": close_price,
            "pre_close": float(getattr(row, "pre_close", 0) or 0),
            "vol": float(getattr(row, "vol", 0) or 0),
            "amount": float(getattr(row, "amount", 0) or 0),
        }

    return result


# 保留旧函数名作为兼容别名
get_realtime_prices_ak = get_realtime_prices
