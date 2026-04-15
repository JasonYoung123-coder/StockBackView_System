from __future__ import annotations

import pandas as pd

from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "sample_strategy"
    description = "可调参数的样本组合策略：用均线和动量筛选强势股。"
    is_portfolio_strategy = True
    lookback_days = 80

    def __init__(
        self,
        fast_ma: int = 5,
        slow_ma: int = 20,
        rebalance_days: int = 5,
        max_holdings: int = 5,
        min_momentum: float = 0.03,
    ) -> None:
        self.fast_ma = int(fast_ma)
        self.slow_ma = int(slow_ma)
        self.rebalance_days = int(rebalance_days)
        self.max_holdings = int(max_holdings)
        self.min_momentum = float(min_momentum)
        self.position_per_stock = 1.0 / max(self.max_holdings, 1)

    def get_config_schema(self) -> dict:
        return {
            "title": "样本策略参数",
            "fields": [
                {"name": "fast_ma", "label": "买入快线均线", "type": "number", "min": 2, "max": 30, "step": 1, "default": self.fast_ma},
                {"name": "slow_ma", "label": "买入慢线均线", "type": "number", "min": 5, "max": 120, "step": 1, "default": self.slow_ma},
                {"name": "rebalance_days", "label": "调仓周期(交易日)", "type": "number", "min": 1, "max": 20, "step": 1, "default": self.rebalance_days},
                {"name": "max_holdings", "label": "最大持股数", "type": "number", "min": 1, "max": 10, "step": 1, "default": self.max_holdings},
                {"name": "min_momentum", "label": "最小20日动量", "type": "number", "min": -0.2, "max": 0.5, "step": 0.01, "default": self.min_momentum},
            ],
        }

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        frame = market_data.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

        weights = pd.DataFrame(0.0, index=pd.DatetimeIndex(trade_dates), columns=sorted(frame["ts_code"].unique()))
        rebalance_dates = trade_dates[:: max(self.rebalance_days, 1)]
        latest_holdings: list[dict] = []

        for idx, rebalance_date in enumerate(rebalance_dates, start=1):
            self.report_progress(60 + (idx / max(len(rebalance_dates), 1)) * 30, f"样本策略调仓 {idx}/{len(rebalance_dates)}")
            history = frame.loc[frame["trade_date"] <= rebalance_date].copy()
            ranked = self._rank_candidates(history)
            selected = ranked.head(self.max_holdings)
            latest_holdings = selected[["ts_code", "name", "score"]].to_dict("records")

            start_idx = trade_dates.index(rebalance_date)
            end_idx = trade_dates.index(rebalance_dates[idx]) if idx < len(rebalance_dates) else len(trade_dates)
            for segment_date in trade_dates[start_idx:end_idx]:
                for row in selected.itertuples():
                    weights.loc[segment_date, row.ts_code] = self.position_per_stock

        return weights, {
            "rebalance_count": len(rebalance_dates),
            "latest_holdings": latest_holdings,
            "sell_reasons": {},
        }

    def _rank_candidates(self, history: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict] = []
        for ts_code, group in history.groupby("ts_code", sort=False):
            series = group.tail(max(self.slow_ma + 5, 30)).copy()
            if len(series) < max(self.slow_ma, 21):
                continue

            close = pd.to_numeric(series["close"], errors="coerce")
            if close.isna().any():
                continue
            fast = close.rolling(self.fast_ma).mean().iloc[-1]
            slow = close.rolling(self.slow_ma).mean().iloc[-1]
            momentum = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 and close.iloc[-21] > 0 else 0.0
            if pd.isna(fast) or pd.isna(slow):
                continue
            if fast <= slow or momentum < self.min_momentum:
                continue

            rows.append(
                {
                    "ts_code": ts_code,
                    "name": str(series.iloc[-1].get("name") or ts_code),
                    "score": round((momentum * 100) + (fast - slow), 4),
                }
            )

        if not rows:
            return pd.DataFrame(columns=["ts_code", "name", "score"])
        return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
