"""
三因子组合策略：跳空量能衰竭 × 波动率调整区间 × 日内区间扩张

因子来源：因子挖掘系统 S 级因子库
因子组合逻辑：截面 Z-Score 标准化 → ICIR 方向加权 → Top-N 等权持有

因子详情：
  gap_volume_exhaustion   (Open-Ref(Close,1)) / Mean(Vol,10)      Rank IC +0.27  做多
  volatility_adjusted_range (Close-Open) / (Max(High,10)-Min(Low,10))  Rank IC -0.16  做空
  intraday_range_expansion  (High-Low) / Mean(High-Low, 20)        Rank IC -0.23  做空
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "triple_factor_composite"
    description = "三因子组合策略：跳空量能 × 波动率区间 × 区间扩张"
    is_portfolio_strategy = True
    lookback_days = 60

    def __init__(
        self,
        rebalance_days: int = 5,
        max_holdings: int = 10,
        w_gap_vol: float = 1.0,
        w_vol_adj_range: float = -1.0,
        w_range_exp: float = -1.0,
        min_avg_amount: float = 5000.0,
        exclude_st: bool = True,
        winsorize_std: float = 3.0,
    ) -> None:
        self.rebalance_days = int(rebalance_days)
        self.max_holdings = int(max_holdings)
        self.w_gap_vol = float(w_gap_vol)
        self.w_vol_adj_range = float(w_vol_adj_range)
        self.w_range_exp = float(w_range_exp)
        self.min_avg_amount = float(min_avg_amount)
        self.exclude_st = bool(exclude_st)
        self.winsorize_std = float(winsorize_std)
        self.position_per_stock = 1.0 / max(self.max_holdings, 1)

    def get_config_schema(self) -> dict:
        return {
            "title": "三因子组合策略参数",
            "fields": [
                {"name": "rebalance_days", "label": "调仓周期(交易日)", "type": "number", "min": 1, "max": 20, "step": 1, "default": self.rebalance_days},
                {"name": "max_holdings", "label": "最大持股数", "type": "number", "min": 3, "max": 30, "step": 1, "default": self.max_holdings},
                {"name": "w_gap_vol", "label": "跳空量能因子权重(正=做多)", "type": "number", "min": -3.0, "max": 3.0, "step": 0.1, "default": self.w_gap_vol},
                {"name": "w_vol_adj_range", "label": "波动率区间因子权重(负=做空)", "type": "number", "min": -3.0, "max": 3.0, "step": 0.1, "default": self.w_vol_adj_range},
                {"name": "w_range_exp", "label": "区间扩张因子权重(负=做空)", "type": "number", "min": -3.0, "max": 3.0, "step": 0.1, "default": self.w_range_exp},
                {"name": "min_avg_amount", "label": "最小日均成交额(千元)", "type": "number", "min": 0, "max": 50000, "step": 500, "default": self.min_avg_amount},
                {"name": "winsorize_std", "label": "Z-Score截断标准差", "type": "number", "min": 1.0, "max": 5.0, "step": 0.5, "default": self.winsorize_std},
            ],
        }

    # ────────────────── 主入口 ──────────────────

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        frame = market_data.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        for col in ("open", "high", "low", "close", "vol", "amount", "pct_chg"):
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

        self.report_progress(50, "正在计算三因子截面值...")
        frame = self._compute_factors(frame)

        all_codes = sorted(frame["ts_code"].unique())
        weights = pd.DataFrame(0.0, index=pd.DatetimeIndex(trade_dates), columns=all_codes)

        rebalance_dates = trade_dates[:: max(self.rebalance_days, 1)]
        latest_holdings: list[dict] = []

        for idx, rb_date in enumerate(rebalance_dates, start=1):
            pct = 60 + (idx / max(len(rebalance_dates), 1)) * 30
            self.report_progress(pct, f"三因子选股调仓 {idx}/{len(rebalance_dates)}")

            selected = self._select_stocks(frame, rb_date)
            if selected.empty:
                continue

            latest_holdings = selected[["ts_code", "name", "score"]].to_dict("records")

            start_i = trade_dates.index(rb_date)
            end_i = trade_dates.index(rebalance_dates[idx]) if idx < len(rebalance_dates) else len(trade_dates)
            n_selected = len(selected)
            weight_per = 1.0 / max(n_selected, 1)
            for seg_date in trade_dates[start_i:end_i]:
                for row in selected.itertuples():
                    weights.loc[seg_date, row.ts_code] = weight_per

        self.report_progress(95, "因子策略计算完成")
        return weights, {
            "rebalance_count": len(rebalance_dates),
            "latest_holdings": latest_holdings,
            "sell_reasons": {},
        }

    # ────────────────── 因子计算 ──────────────────

    def _compute_factors(self, frame: pd.DataFrame) -> pd.DataFrame:
        g = frame.groupby("ts_code", sort=False)

        frame["_prev_close"] = g["close"].shift(1)
        frame["_avg_vol_10"] = g["vol"].transform(
            lambda s: s.rolling(10, min_periods=5).mean()
        )
        frame["_max_high_10"] = g["high"].transform(
            lambda s: s.rolling(10, min_periods=5).max()
        )
        frame["_min_low_10"] = g["low"].transform(
            lambda s: s.rolling(10, min_periods=5).min()
        )
        frame["_daily_range"] = frame["high"] - frame["low"]
        frame["_avg_range_20"] = g["_daily_range"].transform(
            lambda s: s.rolling(20, min_periods=10).mean()
        )
        frame["_avg_amount_10"] = g["amount"].transform(
            lambda s: s.rolling(10, min_periods=5).mean()
        ) if "amount" in frame.columns else np.nan

        # Factor 1: gap_volume_exhaustion — 隔夜跳空 / 10日均量
        frame["f_gap_vol"] = (
            (frame["open"] - frame["_prev_close"])
            / (frame["_avg_vol_10"] + 1e-12)
        )

        # Factor 2: volatility_adjusted_range — 日内涨跌 / 10日价格通道
        channel = frame["_max_high_10"] - frame["_min_low_10"]
        frame["f_vol_adj_range"] = (
            (frame["close"] - frame["open"])
            / (channel + 1e-12)
        )

        # Factor 3: intraday_range_expansion(V2) — 当日振幅 / 20日均振幅
        frame["f_range_exp"] = (
            frame["_daily_range"]
            / (frame["_avg_range_20"] + 1e-12)
        )

        return frame

    # ────────────────── 截面选股 ──────────────────

    def _select_stocks(self, frame: pd.DataFrame, rb_date: pd.Timestamp) -> pd.DataFrame:
        snap = frame.loc[frame["trade_date"] == rb_date].copy()
        if snap.empty:
            return pd.DataFrame(columns=["ts_code", "name", "score"])

        snap = snap.dropna(subset=["f_gap_vol", "f_vol_adj_range", "f_range_exp"])

        if self.exclude_st and "name" in snap.columns:
            snap = snap[~snap["name"].astype(str).str.contains("ST", case=False, na=False)]

        if self.min_avg_amount > 0 and "_avg_amount_10" in snap.columns:
            snap = snap[snap["_avg_amount_10"] >= self.min_avg_amount]

        if "pct_chg" in snap.columns:
            snap = snap[(snap["pct_chg"] < 9.7) & (snap["pct_chg"] > -9.7)]

        if len(snap) < max(self.max_holdings, 3):
            return pd.DataFrame(columns=["ts_code", "name", "score"])

        cap = self.winsorize_std
        for col in ("f_gap_vol", "f_vol_adj_range", "f_range_exp"):
            mu = snap[col].mean()
            sd = snap[col].std()
            if sd > 1e-12:
                snap[f"z_{col}"] = ((snap[col] - mu) / sd).clip(-cap, cap)
            else:
                snap[f"z_{col}"] = 0.0

        snap["score"] = (
            self.w_gap_vol * snap["z_f_gap_vol"]
            + self.w_vol_adj_range * snap["z_f_vol_adj_range"]
            + self.w_range_exp * snap["z_f_range_exp"]
        )

        top = snap.nlargest(self.max_holdings, "score")
        name_col = top["name"].astype(str) if "name" in top.columns else top["ts_code"]
        result = pd.DataFrame({
            "ts_code": top["ts_code"].values,
            "name": name_col.values,
            "score": top["score"].round(4).values,
        })
        return result
