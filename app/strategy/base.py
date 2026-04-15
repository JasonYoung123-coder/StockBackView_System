from __future__ import annotations

from abc import ABC

import pandas as pd


class BaseStrategy(ABC):
    name = "BaseStrategy"
    description = "策略基类"
    is_portfolio_strategy = False
    lookback_days = 60

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data.copy()

    def generate_signals(self, data: pd.DataFrame) -> pd.Series | pd.DataFrame:
        raise NotImplementedError("当前策略未实现单标的信号生成。")

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        raise NotImplementedError("当前策略未实现组合权重生成。")

    def set_progress_callback(self, callback) -> None:
        self._progress_callback = callback

    def report_progress(self, progress: float, message: str = "") -> None:
        callback = getattr(self, "_progress_callback", None)
        if callable(callback):
            callback(progress, message)

    def get_config_schema(self) -> dict | None:
        return None
