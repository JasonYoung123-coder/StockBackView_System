from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "知行选股交易策略"
    description = (
        "基于知行趋势线和KDJ指标选股，仅买入7分满分股票，"
        "跌破多空线止损，放量滞涨止盈，最多持仓5只。"
    )
    is_portfolio_strategy = True
    lookback_days = 200

    max_holdings = 5
    position_per_stock = 0.2

    # ── 知行多空线参数 ──
    M1 = 14
    M2 = 28
    M3 = 57
    M4 = 114

    # ── 选股阈值 ──
    kdj_j_threshold = 13.0
    price_diff_threshold = 2.0
    min_circulating_cap = 20.0
    buy_score_threshold = 7

    # ── 止损：连续2日收盘价低于知行多空线 且低于近30日最低价 ──
    stop_loss_consecutive_days = 2
    stop_loss_low_lookback = 30

    # ── 止盈：涨幅 < 3% 且成交量为近10日最高 ──
    take_profit_rise_limit = 0.03
    take_profit_volume_lookback = 10

    # ── 持仓优化：满仓时用7分新股替换表现不佳的旧持仓 ──
    replace_min_holding_days = 10
    replace_max_return = 0.05

    # ─────────────────────── 主入口 ───────────────────────

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        if market_data.empty or not trade_dates:
            return pd.DataFrame(), {}

        self.report_progress(50, "正在预处理市场数据")
        prepared = self._prepare_market_data(market_data)

        self.report_progress(55, "正在预计算技术指标与评分")
        self._precompute_indicators(prepared)

        self._stock_groups: dict[str, pd.DataFrame] = {
            ts_code: group
            for ts_code, group in prepared.groupby("ts_code", sort=False)
        }

        trade_index = pd.DatetimeIndex(trade_dates)
        date_to_idx: dict[pd.Timestamp, int] = {d: i for i, d in enumerate(trade_index)}
        all_codes = sorted(prepared["ts_code"].unique())
        weights = pd.DataFrame(0.0, index=trade_index, columns=all_codes)

        holdings: dict[str, dict] = {}
        latest_holdings: list[dict] = []
        sell_reasons: dict[str, str] = {}
        sell_prices: dict[str, float] = {}
        rebalance_count = 0

        total_days = len(trade_index)
        for idx, trade_date in enumerate(trade_index, start=1):
            progress = 60.0 + (idx / total_days) * 30.0
            self.report_progress(progress, f"正在执行策略回测 {idx}/{total_days}")

            # ── 卖出判断 ──
            for ts_code in list(holdings.keys()):
                should_sell, reason, custom_price = self._should_sell(
                    ts_code, trade_date, holdings[ts_code],
                )
                if should_sell:
                    key = f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"
                    sell_reasons[key] = reason
                    if custom_price is not None:
                        sell_prices[key] = custom_price
                    holdings.pop(ts_code, None)

            # ── 买入判断 ──
            if len(holdings) < self.max_holdings:
                added = self._fill_positions(prepared, trade_date, holdings)
                if added > 0:
                    rebalance_count += 1

            # ── 持仓优化：满仓时替换持仓久且浮盈低的股票 ──
            if len(holdings) >= self.max_holdings:
                replaced = self._try_replace_holdings(
                    prepared, trade_date, holdings, sell_reasons, date_to_idx,
                )
                if replaced > 0:
                    rebalance_count += 1

            # ── 写入权重 ──
            for ts_code in holdings:
                weights.loc[trade_date, ts_code] = self.position_per_stock
            latest_holdings = self._build_latest_holdings(holdings)

        return weights, {
            "rebalance_count": rebalance_count,
            "latest_holdings": latest_holdings,
            "sell_reasons": sell_reasons,
            "sell_prices": sell_prices,
        }

    # ─────────────────────── 数据预处理 ───────────────────────

    def _prepare_market_data(self, market_data: pd.DataFrame) -> pd.DataFrame:
        frame = market_data.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        frame["name"] = frame["name"].fillna("")
        frame["industry"] = frame["industry"].fillna("未知")
        frame["circ_mv"] = pd.to_numeric(frame["circ_mv"], errors="coerce")

        for col in ("open", "high", "low", "close", "vol", "amount"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

        block_words = ["ST", "*ST", "S*ST", "SST", "退", "N ", "C "]
        for word in block_words:
            frame = frame[~frame["name"].str.contains(word, na=False, regex=False)]

        frame = frame.dropna(subset=["open", "close", "high", "low", "vol"])
        frame = frame.reset_index(drop=True)

        frame["daily_return"] = (
            frame.groupby("ts_code", sort=False)["close"].pct_change().fillna(0.0)
        )
        return frame

    # ─────────────────── 一次性预计算全部指标与评分 ───────────────────

    def _precompute_indicators(self, frame: pd.DataFrame) -> None:
        ts_col = frame["ts_code"]

        def _gt(col: str, func):
            return frame.groupby(ts_col, sort=False)[col].transform(func)

        # ── 知行短期趋势线: EMA(EMA(close, 10), 10) ──
        _ema10 = _gt("close", lambda s: s.ewm(span=10, adjust=False).mean())
        frame["short_trend"] = _ema10.groupby(ts_col, sort=False).transform(
            lambda s: s.ewm(span=10, adjust=False).mean()
        )

        # ── 知行多空线: (MA(M1) + MA(M2) + MA(M3) + MA(M4)) / 4 ──
        _ma_sum = pd.Series(0.0, index=frame.index, dtype=float)
        for period in (self.M1, self.M2, self.M3, self.M4):
            _ma_sum = _ma_sum + _gt("close", lambda s, p=period: s.rolling(p).mean())
        frame["longshort_line"] = _ma_sum / 4

        # ── KDJ J 值 ──
        _low_n = _gt("low", lambda s: s.rolling(9, min_periods=1).min())
        _high_n = _gt("high", lambda s: s.rolling(9, min_periods=1).max())
        _denom = (_high_n - _low_n).replace(0.0, pd.NA)
        _rsv = ((frame["close"] - _low_n) / _denom * 100).fillna(0.0)
        _k = _rsv.groupby(ts_col, sort=False).transform(
            lambda s: s.ewm(com=2, adjust=False).mean()
        )
        _d = _k.groupby(ts_col, sort=False).transform(
            lambda s: s.ewm(com=2, adjust=False).mean()
        )
        frame["kdj_j"] = 3 * _k - 2 * _d

        # ── 量价同向天数 (15日窗口) ──
        _price_diff = _gt("close", lambda s: s.diff())
        _vol_diff = _gt("vol", lambda s: s.diff())
        _same_dir = (
            ((_price_diff > 0) & (_vol_diff > 0))
            | ((_price_diff < 0) & (_vol_diff < 0))
        )
        frame["vol_pattern_15d"] = (
            _same_dir.astype(float)
            .groupby(ts_col, sort=False)
            .transform(lambda s: s.rolling(15, min_periods=1).sum())
        )

        # ── 近30日最低价 (用于止损) ──
        frame["low_30d"] = _gt("low", lambda s: s.rolling(self.stop_loss_low_lookback, min_periods=1).min())

        # ── 近20日振幅: (最高价 - 最低价) / 最低价 (用于排除异常波动股) ──
        _high_20d = _gt("high", lambda s: s.rolling(20, min_periods=20).max())
        _low_20d = _gt("low", lambda s: s.rolling(20, min_periods=20).min())
        frame["amplitude_20d"] = ((_high_20d - _low_20d) / _low_20d.replace(0.0, pd.NA)).fillna(0.0)

        # ── 成交量日变化率 (用于评分3) ──
        frame["vol_pct_change"] = _gt("vol", lambda s: s.pct_change()).fillna(0.0)

        # ── 评分项2: 近20日是否出现金叉 ──
        _prev_short = frame.groupby(ts_col, sort=False)["short_trend"].shift(1)
        _prev_long = frame.groupby(ts_col, sort=False)["longshort_line"].shift(1)
        _cross_up = (
            (frame["short_trend"] >= frame["longshort_line"]) & (_prev_short < _prev_long)
        )
        frame["has_golden_cross_20d"] = (
            _cross_up.astype(float)
            .groupby(ts_col, sort=False)
            .transform(lambda s: s.rolling(20, min_periods=1).max())
            .fillna(0.0)
            .astype(bool)
        )

        # ── 评分项3: 近20日是否出现放量涨停(涨>4%且量>30%且短趋势<多空线) ──
        _burst = (
            (frame["daily_return"] > 0.04)
            & (frame["vol_pct_change"] > 0.30)
            & (frame["short_trend"] < frame["longshort_line"])
        )
        frame["has_burst_20d"] = (
            _burst.astype(float)
            .groupby(ts_col, sort=False)
            .transform(lambda s: s.rolling(20, min_periods=1).max())
            .fillna(0.0)
            .astype(bool)
        )

        # ── 评分项4: 近40日是否出现缩量(连续>=3日量缩 或 最小量/最大量<20%) ──
        _vol_decrease = (_gt("vol", lambda s: s.diff()) < 0).astype(float)
        _consec_3 = (
            _vol_decrease.groupby(ts_col, sort=False)
            .transform(lambda s: s.rolling(3, min_periods=3).sum())
            >= 3
        )
        _has_consec_40d = (
            _consec_3.astype(float)
            .groupby(ts_col, sort=False)
            .transform(lambda s: s.rolling(40, min_periods=1).max())
            .fillna(0.0)
            .astype(bool)
        )
        _vol_min_40 = _gt("vol", lambda s: s.rolling(40, min_periods=40).min())
        _vol_max_40 = _gt("vol", lambda s: s.rolling(40, min_periods=40).max())
        _ratio_below_20 = (
            (_vol_min_40 / _vol_max_40.replace(0.0, pd.NA) * 100) < 20
        ).fillna(False)
        frame["has_vol_shrink_40d"] = _has_consec_40d | _ratio_below_20

    # ─────────────────────── 卖出逻辑 ───────────────────────

    def _should_sell(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        holding: dict,
    ) -> tuple[bool, str, float | None]:
        """返回 (是否卖出, 卖出原因, 自定义卖出价格)；价格为 None 时由引擎按默认逻辑定价。"""
        stock_data = self._stock_groups.get(ts_code)
        if stock_data is None or stock_data.empty:
            return True, "数据缺失", None

        recent = stock_data.loc[stock_data["trade_date"] <= trade_date]
        if len(recent) < 2:
            return False, "", None

        # ── 止损: 连续N天收盘价 < 知行多空线 且 < 前30日最低价 ──
        # low_30d 包含当天最低价，而 low <= close 恒成立，
        # 所以取前一天的 low_30d 作为参考值来排除当天。
        n = self.stop_loss_consecutive_days
        if len(recent) >= n + 1:
            tail = recent.tail(n)
            if tail["longshort_line"].notna().all():
                below_longshort = tail["close"].values < tail["longshort_line"].values
                if below_longshort.all():
                    ref = recent.iloc[-(n + 1):-1]
                    if ref["low_30d"].notna().all():
                        below_low_ref = tail["close"].values < ref["low_30d"].values
                        if below_low_ref.all():
                            return True, "止损-跌破多空线及近期低点", None

        # ── 止盈: 当日涨幅 < 3%，且成交量为近10日最高 ──
        latest = recent.iloc[-1]
        daily_ret = float(latest["daily_return"])
        if 0<daily_ret < self.take_profit_rise_limit:
            lookback = self.take_profit_volume_lookback
            vol_tail = recent.tail(lookback)
            if len(vol_tail) >= lookback:
                if float(latest["vol"]) >= float(vol_tail["vol"].max()):
                    sell_price = float(latest["close"]) - 0.01
                    return True, "止盈-放量滞涨", sell_price

        return False, "", None

    # ─────────────────────── 买入逻辑 ───────────────────────

    def _fill_positions(
        self,
        prepared: pd.DataFrame,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
    ) -> int:
        available = self.max_holdings - len(holdings)
        if available <= 0:
            return 0

        candidates = self._rank_candidates(prepared, trade_date, holdings)
        if candidates.empty:
            return 0

        added = 0
        for row in candidates.itertuples():
            if row.ts_code in holdings:
                continue
            holdings[row.ts_code] = {
                "ts_code": row.ts_code,
                "name": row.name,
                "industry": row.industry,
                "score": float(row.score),
                "selection_reason": row.condition,
                "buy_date": trade_date,
                "buy_price": float(row.current_price),
                "weight": self.position_per_stock,
            }
            added += 1
            if added >= available:
                break
        return added

    def _rank_candidates(
        self,
        prepared: pd.DataFrame,
        as_of_date: pd.Timestamp,
        holdings: dict[str, dict],
    ) -> pd.DataFrame:
        day = prepared.loc[prepared["trade_date"] == as_of_date]
        if day.empty:
            return pd.DataFrame()

        held = set(holdings.keys())

        # 向量化预筛选：一次性排除绝大部分不符合条件的股票
        mask = (
            (~day["ts_code"].isin(held))
            & (day["short_trend"].notna())
            & (day["longshort_line"].notna())
            & (day["kdj_j"].notna())
            & (day["short_trend"] > day["longshort_line"])
            & (day["kdj_j"] < self.kdj_j_threshold)
            & (day["circ_mv"].notna())
            & (day["circ_mv"] >= self.min_circulating_cap)
            & (day["vol_pattern_15d"] >= 10)
            & (day["amplitude_20d"] <= 0.40)
        )
        c = day.loc[mask].copy()
        if c.empty:
            return pd.DataFrame()

        # 选股条件1 / 条件2
        p2s = (c["close"] - c["short_trend"]).abs() / c["short_trend"] * 100
        p2l = (c["close"] - c["longshort_line"]).abs() / c["longshort_line"] * 100

        cond1 = (c["close"] >= c["short_trend"]) | (p2s < 1.0)
        cond2 = (
            (c["close"] < c["short_trend"])
            & (c["close"] > c["longshort_line"])
            & (p2l < self.price_diff_threshold)
        )
        c = c.loc[cond1 | cond2].copy()
        if c.empty:
            return pd.DataFrame()

        # 向量化评分
        j_vals = c["kdj_j"].values
        c["score"] = (
            np.where(j_vals < 0, 2, np.where(j_vals < 13, 1, 0))
            + c["has_golden_cross_20d"].astype(int).values * 2
            + c["has_burst_20d"].astype(int).values * 2
            + c["has_vol_shrink_40d"].astype(int).values
        )

        c = c.loc[c["score"] >= self.buy_score_threshold].copy()
        if c.empty:
            return pd.DataFrame()

        # 补充输出列
        _p2s = (c["close"] - c["short_trend"]).abs() / c["short_trend"] * 100
        _p2l = (c["close"] - c["longshort_line"]).abs() / c["longshort_line"] * 100
        _cond1 = (c["close"] >= c["short_trend"]) | (_p2s < 1.0)
        c["condition"] = np.where(_cond1.values, "条件1", "条件2")
        c["current_price"] = c["close"]
        c["price_diff_ratio"] = _p2l

        return c.sort_values(
            by=["score", "kdj_j", "price_diff_ratio"],
            ascending=[False, True, True],
        ).reset_index(drop=True)

    # ─────────────────────── 持仓优化 ───────────────────────

    def _try_replace_holdings(
        self,
        prepared: pd.DataFrame,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
        sell_reasons: dict[str, str],
        date_to_idx: dict[pd.Timestamp, int],
    ) -> int:
        candidates = self._rank_candidates(prepared, trade_date, holdings)
        if candidates.empty:
            return 0

        current_idx = date_to_idx.get(trade_date, 0)
        replaceable: list[tuple[str, float]] = []
        for ts_code, holding in holdings.items():
            buy_idx = date_to_idx.get(holding["buy_date"])
            if buy_idx is None:
                continue
            if current_idx - buy_idx < self.replace_min_holding_days:
                continue

            current_price = self._get_current_price(ts_code, trade_date)
            if current_price is None:
                continue

            unrealized_return = current_price / holding["buy_price"] - 1.0
            if unrealized_return >= self.replace_max_return:
                continue

            replaceable.append((ts_code, unrealized_return))

        if not replaceable:
            return 0

        replaceable.sort(key=lambda x: x[1])

        replaced = 0
        candidate_iter = candidates.itertuples()
        for old_code, _ in replaceable:
            try:
                new_row = next(candidate_iter)
            except StopIteration:
                break

            sell_reasons[f"{old_code}|{trade_date.strftime('%Y-%m-%d')}"] = "调仓-替换为更优股票"
            holdings.pop(old_code, None)

            holdings[new_row.ts_code] = {
                "ts_code": new_row.ts_code,
                "name": new_row.name,
                "industry": new_row.industry,
                "score": float(new_row.score),
                "selection_reason": new_row.condition,
                "buy_date": trade_date,
                "buy_price": float(new_row.current_price),
                "weight": self.position_per_stock,
            }
            replaced += 1

        return replaced

    def _get_current_price(
        self, ts_code: str, trade_date: pd.Timestamp
    ) -> float | None:
        stock_data = self._stock_groups.get(ts_code)
        if stock_data is None:
            return None
        row = stock_data.loc[stock_data["trade_date"] == trade_date]
        if row.empty:
            return None
        return float(row.iloc[-1]["close"])

    # ─────────────────────── 辅助方法 ───────────────────────

    @staticmethod
    def _build_latest_holdings(holdings: dict[str, dict]) -> list[dict]:
        rows = sorted(holdings.values(), key=lambda h: (-h["score"], h["ts_code"]))
        return [
            {
                "ts_code": h["ts_code"],
                "name": h["name"],
                "industry": h.get("industry", "未知"),
                "score": round(float(h["score"]), 2),
                "selection_reason": h.get("selection_reason", ""),
            }
            for h in rows
        ]
