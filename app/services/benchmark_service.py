from __future__ import annotations

import pandas as pd

from app.core.config import get_settings
from app.services.tushare_client import TushareClient


class BenchmarkService:
    def __init__(self, tushare_client: TushareClient | None = None) -> None:
        self.settings = get_settings()
        self.tushare_client = tushare_client or TushareClient()

    def get_benchmarks(
        self,
        start_date: str,
        end_date: str,
        base_dates: pd.DatetimeIndex,
    ) -> dict[str, pd.DataFrame]:
        results: dict[str, pd.DataFrame] = {}
        for name, ts_code in self.settings.benchmarks.items():
            frame = self.tushare_client.get_index_daily(ts_code, start_date, end_date)
            indexed = frame.set_index("trade_date").sort_index()
            indexed = indexed.reindex(base_dates).ffill().dropna(subset=["close"])
            indexed = indexed.reset_index().rename(columns={"index": "trade_date"})
            results[name] = indexed
        return results
