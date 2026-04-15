from __future__ import annotations

from datetime import timedelta

import pandas as pd

from app.services.tushare_client import TushareClient


class MarketDataService:
    def __init__(self, tushare_client: TushareClient | None = None) -> None:
        self.tushare_client = tushare_client or TushareClient()

    def get_trade_dates(self, start_date: str, end_date: str) -> list[pd.Timestamp]:
        calendar = self.tushare_client.get_trade_calendar(start_date, end_date)
        if calendar.empty:
            return []
        calendar["is_open"] = pd.to_numeric(calendar["is_open"], errors="coerce").fillna(0)
        open_days = calendar.loc[calendar["is_open"] == 1, "cal_date"]
        return sorted(pd.to_datetime(open_days).tolist())

    def get_market_history(
        self,
        start_date: str,
        end_date: str,
        lookback_days: int = 80,
        progress_callback=None,
    ) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
        start_dt = pd.to_datetime(start_date) - timedelta(days=max(lookback_days * 3, 90))
        history_trade_dates = self.get_trade_dates(start_dt.strftime("%Y%m%d"), end_date)
        if not history_trade_dates:
            raise RuntimeError("未获取到回测区间对应的交易日。")

        stock_basic = self.tushare_client.get_stock_basic()
        frames: list[pd.DataFrame] = []
        total_days = len(history_trade_dates)

        for index, trade_day in enumerate(history_trade_dates, start=1):
            trade_day_str = trade_day.strftime("%Y%m%d")
            daily = self.tushare_client.get_market_daily_by_trade_date(trade_day_str)
            if daily.empty:
                if callable(progress_callback):
                    progress_callback(15 + (index / max(total_days, 1)) * 30, f"正在加载市场数据 {index}/{total_days}")
                continue

            basic = self.tushare_client.get_daily_basic_by_trade_date(trade_day_str)
            moneyflow = self.tushare_client.get_moneyflow_by_trade_date(trade_day_str)
            adj = self.tushare_client.get_adj_factor_by_trade_date(trade_day_str)

            frame = daily.merge(
                basic.reindex(columns=["ts_code", "trade_date", "turnover_rate", "circ_mv"]) if not basic.empty else basic,
                on=["ts_code", "trade_date"],
                how="left",
            )
            frame = frame.merge(
                moneyflow.reindex(columns=["ts_code", "trade_date", "net_mf_amount"]) if not moneyflow.empty else moneyflow,
                on=["ts_code", "trade_date"],
                how="left",
            )
            frame = frame.merge(
                adj.reindex(columns=["ts_code", "trade_date", "adj_factor"]) if not adj.empty else adj,
                on=["ts_code", "trade_date"],
                how="left",
            )
            frames.append(frame)
            if callable(progress_callback):
                progress_callback(15 + (index / max(total_days, 1)) * 30, f"正在加载市场数据 {index}/{total_days}")

        if not frames:
            raise RuntimeError("未拉取到全市场历史数据。")

        market = pd.concat(frames, ignore_index=True)
        market = market.merge(stock_basic, on="ts_code", how="left")
        market["trade_date"] = pd.to_datetime(market["trade_date"])
        market = market.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

        for col in ("open", "high", "low", "close"):
            market[col] = pd.to_numeric(market[col], errors="coerce")
        market["close_unadj"] = market["close"].copy()

        if "adj_factor" in market.columns:
            market["adj_factor"] = pd.to_numeric(market["adj_factor"], errors="coerce")
            latest_adj = market.groupby("ts_code")["adj_factor"].transform("last")
            ratio = market["adj_factor"] / latest_adj
            ratio = ratio.replace([float("inf"), float("-inf")], pd.NA).fillna(1.0)
            for col in ("open", "high", "low", "close"):
                market[col] = market[col] * ratio

        market["turnover_rate"] = pd.to_numeric(market.get("turnover_rate"), errors="coerce")
        market["circ_mv"] = pd.to_numeric(market.get("circ_mv"), errors="coerce")
        market["net_mf_amount"] = pd.to_numeric(market.get("net_mf_amount"), errors="coerce")
        backtest_trade_dates = [day for day in history_trade_dates if pd.to_datetime(start_date) <= day <= pd.to_datetime(end_date)]
        return market, backtest_trade_dates
