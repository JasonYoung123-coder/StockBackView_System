from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app.services.tushare_client import TushareClient
from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "劲帆量化模型2.0.1"
    description = (
        "基于劲帆小弟线和KDJ指标选股，仅买入7分满分股票，"
        "结合活跃市值状态优化持仓节奏，跌破大哥线止损，放量滞涨止盈，最多持仓5只。"
    )
    is_portfolio_strategy = True
    lookback_days = 200

    max_holdings = 5
    position_per_stock = 0.2

    # ── 劲帆大哥线参数 ──
    M1 = 14
    M2 = 28
    M3 = 57
    M4 = 114

    # ── 选股阈值 ──
    kdj_j_threshold = 13.0
    price_diff_threshold = 2.0
    min_circulating_cap = 20.0
    buy_score_threshold = 7
    force_full_regimes = {"大涨", "金叉"}

    # ── 止损：连续2日收盘价低于劲帆大哥线 且低于近30日最低价 ──
    stop_loss_consecutive_days = 2
    stop_loss_low_lookback = 25

    # ── 止盈：盈利筹码占比 > 99%/前日>98% 且成交量为近30日最高 ──
    take_profit_profit_threshold = 99.0
    take_profit_prev_day_threshold = 99.0
    take_profit_volume_lookback = 30
    take_profit_near_threshold_gap = 2.0
    chips_prefetch_days = 60

    # ── 持仓优化：满仓时用7分新股替换表现不佳的旧持仓 ──
    replace_min_holding_days = 9
    replace_max_return = 0.05
    oamv_big_rise_window = 6
    oamv_signal_file = Path(__file__).resolve().parents[1] / "analysis" / "OAMV_MACD.xlsx"
    chips_fallback_file = Path(__file__).resolve().parents[1] / "analysis" / "chips_data.xlsx"

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

        self._tushare = TushareClient()
        self._chips_cache: dict[str, pd.DataFrame] = {}
        self._chips_fetched_end: dict[str, str] = {}
        self._chips_missing_attempts: dict[str, set[str]] = {}
        self._chips_full_range_requested: set[str] = set()
        self._chips_fallback_map = self._load_local_chips_fallback()
        self._warnings: list[str] = []
        self._warning_keys: set[str] = set()

        trade_index = pd.DatetimeIndex(trade_dates)
        self._backtest_start_date = pd.Timestamp(trade_index.min()).normalize()
        self._backtest_end_date = pd.Timestamp(trade_index.max()).normalize()
        self._trade_dates_list: list[pd.Timestamp] = list(trade_index)
        self._date_to_list_idx: dict[pd.Timestamp, int] = {
            d: i for i, d in enumerate(self._trade_dates_list)
        }
        self._oamv_regime = self._load_oamv_regime(trade_index)
        date_to_idx: dict[pd.Timestamp, int] = self._date_to_list_idx
        all_codes = sorted(prepared["ts_code"].unique())
        weights = pd.DataFrame(0.0, index=trade_index, columns=all_codes)

        holdings: dict[str, dict] = {}
        latest_holdings: list[dict] = []
        sell_reasons: dict[str, str] = {}
        sell_prices: dict[str, float] = {}
        sell_orders: dict[str, dict[str, object]] = {}
        buy_prices: dict[str, float] = {}
        rebalance_count = 0

        total_days = len(trade_index)
        for idx, trade_date in enumerate(trade_index, start=1):
            progress = 60.0 + (idx / total_days) * 30.0
            self.report_progress(progress, f"正在执行策略回测 {idx}/{total_days}")
            oamv_regime = self._get_oamv_regime(trade_date)

            # ── 卖出判断 ──
            for ts_code in list(holdings.keys()):
                holding = holdings[ts_code]
                if pd.Timestamp(holding["buy_date"]) >= pd.Timestamp(trade_date):
                    continue
                should_sell, reason, custom_price = self._should_sell(
                    ts_code, trade_date, holding,
                )
                if should_sell:
                    key = f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"
                    sell_reasons[key] = reason
                    if custom_price is not None:
                        sell_prices[key] = custom_price
                    holdings.pop(ts_code, None)
                    self._chips_cache.pop(ts_code, None)
                    self._chips_fetched_end.pop(ts_code, None)
                    self._chips_missing_attempts.pop(ts_code, None)

            if oamv_regime == "死叉":
                forced_exit = self._sell_new_buys_after_dead_cross(
                    trade_date, holdings, sell_reasons, sell_orders,
                )
                if forced_exit > 0:
                    rebalance_count += 1

            # ── 买入判断 ──
            if oamv_regime != "死叉" and len(holdings) < self.max_holdings:
                added = self._fill_positions(prepared, trade_date, holdings, buy_prices, oamv_regime)
                if added > 0:
                    rebalance_count += 1

            # ── 持仓优化：满仓时替换持仓久且浮盈低的股票 ──
            if oamv_regime != "死叉":
                if len(holdings) >= self.max_holdings:
                    replaced = self._try_replace_holdings(
                        prepared, trade_date, holdings, sell_reasons, date_to_idx, buy_prices, oamv_regime,
                    )
                    if replaced > 0:
                        rebalance_count += 1
            elif holdings:
                reduced = self._reduce_holdings_on_dead_cross(
                    prepared, trade_date, holdings, sell_reasons, date_to_idx,
                )
                if reduced > 0:
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
            "sell_orders": sell_orders,
            "buy_prices": buy_prices,
            "warnings": self._warnings,
            "chips_data_source_report": self._tushare.get_chips_data_source_report(),
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

        # ── 劲帆小弟线: EMA(EMA(close, 10), 10) ──
        _ema10 = _gt("close", lambda s: s.ewm(span=10, adjust=False).mean())
        frame["short_trend"] = _ema10.groupby(ts_col, sort=False).transform(
            lambda s: s.ewm(span=10, adjust=False).mean()
        )

        # ── 劲帆大哥线: (MA(M1) + MA(M2) + MA(M3) + MA(M4)) / 4 ──
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

        # ── 评分项3: 近20日是否出现放量涨停(涨>4%且量>30%且小弟线<大哥线) ──
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

        # ── 止损: 连续N天收盘价 < 劲帆大哥线 且 < 前30日最低价 ──
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
                            return True, "止损-跌破大哥线及近期低点", None

        # ── 止盈: 盈利筹码占比 > 99% 且成交量为近30日最高 ──
        latest = recent.iloc[-1]
        lookback = self.take_profit_volume_lookback
        vol_tail = recent.tail(lookback)
        is_vol_max = (
            len(vol_tail) >= lookback
            and float(latest["vol"]) >= float(vol_tail["vol"].max())
        )

        if is_vol_max:
            self._ensure_chips(ts_code, holding["buy_date"], trade_date)
            close_price = float(latest["close"])
            chip_price = self._get_chip_reference_price(latest, ts_code)
            threshold = self.take_profit_profit_threshold

            today_ratio = self._get_profit_ratio(ts_code, trade_date, chip_price)
            if today_ratio is None:
                self._add_warning(
                    ts_code,
                    trade_date,
                    "止盈判断缺少当日筹码数据，已跳过“获利筹码占比”检查",
                )
            if today_ratio is not None and today_ratio > threshold:
                return True, "止盈-筹码获利盘过高", close_price - 0.01
            self._warn_near_take_profit(
                ts_code=ts_code,
                trade_date=trade_date,
                ratio=today_ratio,
                label="当日",
                threshold=threshold,
            )

            if len(recent) >= 2:
                prev = recent.iloc[-2]
                prev_chip_price = self._get_chip_reference_price(prev, ts_code)
                prev_ratio = self._get_profit_ratio(
                    ts_code, prev["trade_date"], prev_chip_price,
                )
                if prev_ratio is None:
                    self._add_warning(
                        ts_code,
                        prev["trade_date"],
                        "止盈判断缺少前一日筹码数据，已跳过“前一日获利筹码占比”检查",
                    )
                if prev_ratio is not None and prev_ratio > self.take_profit_prev_day_threshold:
                    return True, "止盈-筹码获利盘过高", close_price - 0.01
                self._warn_near_take_profit(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    ratio=prev_ratio,
                    label="前一日",
                    threshold=self.take_profit_prev_day_threshold,
                )

        return False, "", None

    # ─────────────────────── 买入逻辑 ───────────────────────

    def _fill_positions(
        self,
        prepared: pd.DataFrame,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
        buy_prices: dict[str, float],
        oamv_regime: str,
    ) -> int:
        available = self.max_holdings - len(holdings)
        if available <= 0:
            return 0

        candidates = self._build_fill_candidates(prepared, trade_date, holdings, oamv_regime)
        if candidates.empty:
            return 0

        added = 0
        for row in candidates.itertuples():
            if row.ts_code in holdings:
                continue
            execution_date, execution_price = self._get_buy_execution(row.ts_code, trade_date)
            if execution_date is None or execution_price is None:
                continue
            holdings[row.ts_code] = {
                "ts_code": row.ts_code,
                "name": row.name,
                "industry": row.industry,
                "score": float(row.score),
                "selection_reason": f"{row.condition}{getattr(row, 'reason_suffix', '')}",
                "buy_date": execution_date,
                "buy_price": execution_price,
                "weight": self.position_per_stock,
            }
            self._prefetch_full_backtest_chips(row.ts_code)
            buy_prices[f"{row.ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = execution_price
            added += 1
            if added >= available:
                break
        return added

    def _build_fill_candidates(
        self,
        prepared: pd.DataFrame,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
        oamv_regime: str,
    ) -> pd.DataFrame:
        strict_candidates = self._rank_candidates(prepared, trade_date, holdings).copy()
        if strict_candidates.empty:
            strict_candidates = pd.DataFrame()
        if not strict_candidates.empty:
            strict_candidates["reason_suffix"] = ""

        if oamv_regime not in self.force_full_regimes:
            return strict_candidates.reset_index(drop=True)

        available = self.max_holdings - len(holdings)
        selected_frames: list[pd.DataFrame] = []
        seen_codes: set[str] = set()

        if not strict_candidates.empty:
            selected_frames.append(strict_candidates)
            seen_codes.update(strict_candidates["ts_code"].tolist())

        remaining = available - len(seen_codes)
        if remaining > 0:
            relaxed_candidates = self._rank_candidates(
                prepared, trade_date, holdings, min_score=0,
            ).copy()
            if not relaxed_candidates.empty:
                relaxed_candidates = relaxed_candidates.loc[
                    ~relaxed_candidates["ts_code"].isin(seen_codes)
                ].copy()
                if not relaxed_candidates.empty:
                    relaxed_candidates["reason_suffix"] = "-OAMV多头补满仓"
                    selected_frames.append(relaxed_candidates)
                    seen_codes.update(relaxed_candidates["ts_code"].tolist())

        remaining = available - len(seen_codes)
        if remaining > 0:
            fallback_candidates = self._rank_fallback_candidates(
                prepared, trade_date, holdings,
            ).copy()
            if not fallback_candidates.empty:
                fallback_candidates = fallback_candidates.loc[
                    ~fallback_candidates["ts_code"].isin(seen_codes)
                ].copy()
                if not fallback_candidates.empty:
                    fallback_candidates["reason_suffix"] = "-OAMV多头兜底补仓"
                    selected_frames.append(fallback_candidates)

        if not selected_frames:
            return pd.DataFrame()

        combined = pd.concat(selected_frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["ts_code"], keep="first")
        return combined.head(available).reset_index(drop=True)

    def _rank_candidates(
        self,
        prepared: pd.DataFrame,
        as_of_date: pd.Timestamp,
        holdings: dict[str, dict],
        min_score: float | None = None,
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

        score_threshold = self.buy_score_threshold if min_score is None else min_score
        if score_threshold is not None:
            c = c.loc[c["score"] >= score_threshold].copy()
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

    def _rank_fallback_candidates(
        self,
        prepared: pd.DataFrame,
        as_of_date: pd.Timestamp,
        holdings: dict[str, dict],
    ) -> pd.DataFrame:
        day = prepared.loc[prepared["trade_date"] == as_of_date]
        if day.empty:
            return pd.DataFrame()

        held = set(holdings.keys())
        c = day.loc[
            (~day["ts_code"].isin(held))
            & (day["short_trend"].notna())
            & (day["longshort_line"].notna())
            & (day["kdj_j"].notna())
            & (day["circ_mv"].notna())
            & (day["circ_mv"] >= self.min_circulating_cap)
        ].copy()
        if c.empty:
            return pd.DataFrame()

        j_vals = c["kdj_j"].values
        c["score"] = (
            np.where(j_vals < 0, 2, np.where(j_vals < 13, 1, 0))
            + c["has_golden_cross_20d"].astype(int).values * 2
            + c["has_burst_20d"].astype(int).values * 2
            + c["has_vol_shrink_40d"].astype(int).values
        )
        c["condition"] = "空仓兜底"
        c["current_price"] = c["close"]
        c["price_diff_ratio"] = (
            (c["close"] - c["longshort_line"]).abs()
            / c["longshort_line"].replace(0.0, pd.NA)
            * 100
        ).fillna(999.0)
        c["trend_priority"] = (c["short_trend"] > c["longshort_line"]).astype(int)

        return c.sort_values(
            by=["score", "trend_priority", "kdj_j", "price_diff_ratio"],
            ascending=[False, False, True, True],
        ).reset_index(drop=True)

    # ─────────────────────── 持仓优化 ───────────────────────

    def _try_replace_holdings(
        self,
        prepared: pd.DataFrame,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
        sell_reasons: dict[str, str],
        date_to_idx: dict[pd.Timestamp, int],
        buy_prices: dict[str, float],
        oamv_regime: str,
    ) -> int:
        replaced = 0
        if oamv_regime in self.force_full_regimes:
            replaced += self._replace_losing_non_top_score_holdings(
                prepared, trade_date, holdings, sell_reasons, buy_prices,
            )

        candidates = self._rank_candidates(prepared, trade_date, holdings)
        if candidates.empty:
            return replaced
        replaceable = self._get_replaceable_holdings(holdings, trade_date, date_to_idx)
        if not replaceable:
            return replaced

        replaceable.sort(key=lambda x: x[1])

        candidate_iter = candidates.itertuples()
        for old_code, _ in replaceable:
            new_row = None
            execution_date = None
            execution_price = None
            for candidate in candidate_iter:
                execution_date, execution_price = self._get_buy_execution(candidate.ts_code, trade_date)
                if execution_date is None or execution_price is None:
                    continue
                new_row = candidate
                break

            if new_row is None or execution_date is None or execution_price is None:
                break

            sell_reasons[f"{old_code}|{trade_date.strftime('%Y-%m-%d')}"] = "调仓-替换为更优股票"
            holdings.pop(old_code, None)
            self._chips_cache.pop(old_code, None)
            self._chips_fetched_end.pop(old_code, None)
            self._chips_missing_attempts.pop(old_code, None)

            holdings[new_row.ts_code] = {
                "ts_code": new_row.ts_code,
                "name": new_row.name,
                "industry": new_row.industry,
                "score": float(new_row.score),
                "selection_reason": new_row.condition,
                "buy_date": execution_date,
                "buy_price": execution_price,
                "weight": self.position_per_stock,
            }
            self._prefetch_full_backtest_chips(new_row.ts_code)
            buy_prices[f"{new_row.ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = execution_price
            replaced += 1

        return replaced

    def _replace_losing_non_top_score_holdings(
        self,
        prepared: pd.DataFrame,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
        sell_reasons: dict[str, str],
        buy_prices: dict[str, float],
    ) -> int:
        top_score_candidates = self._rank_candidates(
            prepared,
            trade_date,
            holdings,
            min_score=self.buy_score_threshold,
        )
        if top_score_candidates.empty:
            return 0

        losers = self._get_losing_non_top_score_holdings(holdings, trade_date)
        if not losers:
            return 0

        replaced = 0
        loser_idx = 0
        for candidate in top_score_candidates.itertuples():
            if loser_idx >= len(losers):
                break

            execution_date, execution_price = self._get_buy_execution(candidate.ts_code, trade_date)
            if execution_date is None or execution_price is None:
                continue

            old_code, _ = losers[loser_idx]
            loser_idx += 1
            sell_reasons[f"{old_code}|{trade_date.strftime('%Y-%m-%d')}"] = "调仓-7分股替换亏损低分持仓"
            holdings.pop(old_code, None)
            self._chips_cache.pop(old_code, None)
            self._chips_fetched_end.pop(old_code, None)
            self._chips_missing_attempts.pop(old_code, None)

            holdings[candidate.ts_code] = {
                "ts_code": candidate.ts_code,
                "name": candidate.name,
                "industry": candidate.industry,
                "score": float(candidate.score),
                "selection_reason": candidate.condition,
                "buy_date": execution_date,
                "buy_price": execution_price,
                "weight": self.position_per_stock,
            }
            self._prefetch_full_backtest_chips(candidate.ts_code)
            buy_prices[f"{candidate.ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = execution_price
            replaced += 1

        return replaced

    def _reduce_holdings_on_dead_cross(
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

        replaceable = self._get_replaceable_holdings(holdings, trade_date, date_to_idx)
        if not replaceable:
            return 0

        replaceable.sort(key=lambda x: x[1])
        sell_count = min(len(replaceable), len(candidates))
        reduced = 0
        for old_code, _ in replaceable[:sell_count]:
            sell_reasons[f"{old_code}|{trade_date.strftime('%Y-%m-%d')}"] = "活跃市值死叉-调仓只卖不买"
            holdings.pop(old_code, None)
            self._chips_cache.pop(old_code, None)
            self._chips_fetched_end.pop(old_code, None)
            self._chips_missing_attempts.pop(old_code, None)
            reduced += 1
        return reduced

    def _sell_new_buys_after_dead_cross(
        self,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
        sell_reasons: dict[str, str],
        sell_orders: dict[str, dict[str, object]],
    ) -> int:
        next_trade_date = self._get_next_trade_date(trade_date)
        if next_trade_date is None:
            return 0

        reduced = 0
        for ts_code in list(holdings.keys()):
            holding = holdings[ts_code]
            if pd.Timestamp(holding["buy_date"]) != pd.Timestamp(trade_date):
                continue

            execution_date, execution_price = self._get_next_open_execution(ts_code, trade_date)
            if execution_date is None or execution_price is None:
                continue

            signal_key = f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"
            sell_reasons[signal_key] = "活跃市值死叉-死叉日新买入次日开盘卖出"
            sell_orders[signal_key] = {
                "execution_date": execution_date.strftime("%Y-%m-%d"),
                "execution_price": execution_price,
            }
            holdings.pop(ts_code, None)
            self._chips_cache.pop(ts_code, None)
            self._chips_fetched_end.pop(ts_code, None)
            self._chips_missing_attempts.pop(ts_code, None)
            reduced += 1
        return reduced

    def _get_replaceable_holdings(
        self,
        holdings: dict[str, dict],
        trade_date: pd.Timestamp,
        date_to_idx: dict[pd.Timestamp, int],
    ) -> list[tuple[str, float]]:
        current_idx = date_to_idx.get(trade_date, 0)
        replaceable: list[tuple[str, float]] = []
        for ts_code, holding in holdings.items():
            buy_idx = date_to_idx.get(holding["buy_date"])
            if buy_idx is None or current_idx - buy_idx < self.replace_min_holding_days:
                continue

            current_price = self._get_current_price(ts_code, trade_date)
            if current_price is None:
                continue

            unrealized_return = current_price / holding["buy_price"] - 1.0
            if unrealized_return < self.replace_max_return:
                replaceable.append((ts_code, unrealized_return))
        return replaceable

    def _get_losing_non_top_score_holdings(
        self,
        holdings: dict[str, dict],
        trade_date: pd.Timestamp,
    ) -> list[tuple[str, float]]:
        replaceable: list[tuple[str, float]] = []
        for ts_code, holding in holdings.items():
            if pd.Timestamp(holding["buy_date"]) > pd.Timestamp(trade_date):
                continue
            if float(holding.get("score", 0.0)) >= self.buy_score_threshold:
                continue

            current_price = self._get_current_price(ts_code, trade_date)
            if current_price is None:
                continue

            unrealized_return = current_price / holding["buy_price"] - 1.0
            if unrealized_return < 0:
                replaceable.append((ts_code, unrealized_return))
        replaceable.sort(key=lambda x: x[1])
        return replaceable

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

    def _get_buy_execution(
        self,
        ts_code: str,
        signal_date: pd.Timestamp,
    ) -> tuple[pd.Timestamp | None, float | None]:
        return self._get_next_open_execution(ts_code, signal_date)

    def _get_next_open_execution(
        self,
        ts_code: str,
        signal_date: pd.Timestamp,
    ) -> tuple[pd.Timestamp | None, float | None]:
        next_trade_date = self._get_next_trade_date(signal_date)
        if next_trade_date is None:
            return None, None

        stock_data = self._stock_groups.get(ts_code)
        if stock_data is None:
            return None, None

        row = stock_data.loc[stock_data["trade_date"] == next_trade_date]
        if row.empty:
            return None, None

        open_price = row.iloc[-1].get("open")
        if pd.isna(open_price) or float(open_price) <= 0:
            return None, None
        return next_trade_date, float(open_price)

    def _get_next_trade_date(self, trade_date: pd.Timestamp) -> pd.Timestamp | None:
        idx = self._date_to_list_idx.get(pd.Timestamp(trade_date))
        if idx is None or idx + 1 >= len(self._trade_dates_list):
            return None
        return self._trade_dates_list[idx + 1]

    def _get_prev_trade_date(self, trade_date: pd.Timestamp) -> pd.Timestamp | None:
        idx = self._date_to_list_idx.get(pd.Timestamp(trade_date))
        if idx is None or idx <= 0:
            return None
        return self._trade_dates_list[idx - 1]

    # ─────────────────────── 筹码数据 ───────────────────────

    def _prefetch_full_backtest_chips(self, ts_code: str) -> None:
        if ts_code in self._chips_full_range_requested:
            return
        if not hasattr(self, "_backtest_start_date") or not hasattr(self, "_backtest_end_date"):
            return

        start_str = self._backtest_start_date.strftime("%Y%m%d")
        end_str = self._backtest_end_date.strftime("%Y%m%d")
        try:
            full_data = self._tushare.get_cyq_chips_range(ts_code, start_str, end_str)
        except Exception as exc:  # noqa: BLE001
            self._warn_chips_fetch_issue(
                ts_code,
                self._backtest_end_date,
                f"筹码整段预拉取失败: {exc}",
            )
            self._chips_full_range_requested.add(ts_code)
            return

        existing = self._chips_cache.get(ts_code)
        if existing is not None and not existing.empty and not full_data.empty:
            self._chips_cache[ts_code] = (
                pd.concat([existing, full_data], ignore_index=True)
                .drop_duplicates(subset=["trade_date", "price"], keep="last")
            )
        elif not full_data.empty:
            self._chips_cache[ts_code] = full_data

        merged = self._chips_cache.get(ts_code)
        if merged is not None and not merged.empty:
            self._chips_fetched_end[ts_code] = pd.to_datetime(merged["trade_date"]).max().strftime("%Y%m%d")
        self._chips_full_range_requested.add(ts_code)

    def _ensure_chips(
        self,
        ts_code: str,
        buy_date: pd.Timestamp,
        trade_date: pd.Timestamp,
    ) -> None:
        """确保止盈判断依赖的当日/前一日筹码数据尽量在缓存中可用。"""
        self._prefetch_full_backtest_chips(ts_code)
        date_str = trade_date.strftime("%Y%m%d")
        fetched_end = self._chips_fetched_end.get(ts_code)

        required_dates = [pd.Timestamp(trade_date).normalize()]
        prev_trade_date = self._get_prev_trade_date(trade_date)
        if prev_trade_date is not None:
            required_dates.append(pd.Timestamp(prev_trade_date).normalize())

        existing = self._chips_cache.get(ts_code)
        cached_dates: set[pd.Timestamp] = set()
        if existing is not None and not existing.empty:
            cached_dates = set(pd.to_datetime(existing["trade_date"]).dt.normalize())

        missing_targets = [dt for dt in required_dates if dt not in cached_dates]
        if not missing_targets and fetched_end is not None and date_str <= fetched_end:
            return

        attempted_missing = self._chips_missing_attempts.setdefault(ts_code, set())
        pending_targets = [
            dt for dt in missing_targets if dt.strftime("%Y%m%d") not in attempted_missing
        ]
        if not pending_targets and missing_targets:
            return

        if pending_targets:
            target_indexes = [
                self._date_to_list_idx.get(pd.Timestamp(dt))
                for dt in pending_targets
            ]
            target_indexes = [idx for idx in target_indexes if idx is not None]
            if not target_indexes:
                start_idx = self._date_to_list_idx.get(pd.Timestamp(buy_date), 0)
                end_idx_target = start_idx
            else:
                start_idx = min(target_indexes)
                end_idx_target = max(target_indexes)
        elif fetched_end is not None:
            last_idx = self._date_to_list_idx.get(pd.Timestamp(fetched_end))
            start_idx = (last_idx + 1) if last_idx is not None else 0
            end_idx_target = start_idx
        else:
            start_idx = self._date_to_list_idx.get(pd.Timestamp(buy_date), 0)
            end_idx_target = start_idx

        dates = self._trade_dates_list
        if start_idx >= len(dates):
            return

        narrow_start = max(start_idx - 2, 0)
        narrow_end = min(end_idx_target + 2, len(dates) - 1)
        start_str = dates[narrow_start].strftime("%Y%m%d")
        end_str = dates[narrow_end].strftime("%Y%m%d")

        try:
            new_data = self._tushare.get_cyq_chips_range(
                ts_code, start_str, end_str, skip_cache=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn_chips_fetch_issue(
                ts_code,
                trade_date,
                f"筹码缺口补拉失败: {exc}",
            )
            for dt in pending_targets:
                attempted_missing.add(dt.strftime("%Y%m%d"))
            return

        if existing is not None and not existing.empty and not new_data.empty:
            self._chips_cache[ts_code] = (
                pd.concat([existing, new_data], ignore_index=True)
                .drop_duplicates(subset=["trade_date", "price"], keep="last")
            )
        elif not new_data.empty:
            self._chips_cache[ts_code] = new_data

        merged = self._chips_cache.get(ts_code)
        merged_dates: set[pd.Timestamp] = set()
        if merged is not None and not merged.empty:
            merged_dates = set(pd.to_datetime(merged["trade_date"]).dt.normalize())
            self._chips_fetched_end[ts_code] = pd.to_datetime(merged["trade_date"]).max().strftime("%Y%m%d")
        elif fetched_end is None:
            self._chips_fetched_end[ts_code] = end_str

        for dt in pending_targets:
            dt_str = dt.strftime("%Y%m%d")
            if dt in merged_dates:
                attempted_missing.discard(dt_str)
            else:
                attempted_missing.add(dt_str)

    def _get_profit_ratio(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        close_price: float,
    ) -> float | None:
        """返回盈利筹码占比 (0~100)；无数据时返回 None."""
        chips = self._chips_cache.get(ts_code)
        if chips is None or chips.empty:
            return self._get_fallback_profit_ratio(ts_code, trade_date)
        td = pd.Timestamp(trade_date)
        day_chips = chips.loc[chips["trade_date"] == td]
        if day_chips.empty:
            return self._get_fallback_profit_ratio(ts_code, trade_date)
        distribution = day_chips.loc[
            pd.to_numeric(day_chips.get("price"), errors="coerce").notna()
            & pd.to_numeric(day_chips.get("percent"), errors="coerce").notna()
        ].copy()
        if not distribution.empty:
            return float(distribution.loc[distribution["price"] <= close_price, "percent"].sum())

        summary_ratio = pd.to_numeric(day_chips.get("profit_ratio"), errors="coerce").dropna()
        if not summary_ratio.empty:
            return float(summary_ratio.iloc[-1])
        return self._get_fallback_profit_ratio(ts_code, trade_date)

    def _get_chip_reference_price(self, row: pd.Series, ts_code: str) -> float:
        """返回与 cyq_chips 筹码分布同口径的前复权收盘价。"""
        return float(row.get("close"))

    # ─────────────────────── 辅助方法 ───────────────────────

    def _load_oamv_regime(self, trade_index: pd.DatetimeIndex) -> pd.Series:
        if not self.oamv_signal_file.exists():
            raise FileNotFoundError(f"未找到活跃市值状态文件: {self.oamv_signal_file}")

        frame = pd.read_excel(self.oamv_signal_file, usecols=[0, 1])
        if frame.empty:
            raise ValueError(f"活跃市值状态文件为空: {self.oamv_signal_file}")

        frame = frame.rename(columns={frame.columns[0]: "trade_date", frame.columns[1]: "status"})
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["status"] = frame["status"].astype(str).str.strip()
        frame = frame.dropna(subset=["trade_date"])
        frame = frame.loc[frame["status"].isin({"金叉", "死叉", "大涨"})].copy()
        if frame.empty:
            raise ValueError(f"活跃市值状态文件缺少有效状态: {self.oamv_signal_file}")

        frame["trade_date"] = frame["trade_date"].dt.normalize()
        frame = frame.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
        event_map = dict(zip(frame["trade_date"], frame["status"]))

        regimes: list[str] = []
        current_state = "死叉"
        strict_state = "死叉"
        pending_dead_idx: int | None = None

        normalized_dates = [pd.Timestamp(day).normalize() for day in trade_index]
        for idx, trade_date in enumerate(normalized_dates):
            event_status = event_map.get(trade_date)
            if event_status == "金叉":
                current_state = "金叉"
                strict_state = "金叉"
                pending_dead_idx = None
            elif event_status == "死叉":
                current_state = "死叉"
                strict_state = "死叉"
                pending_dead_idx = None
            elif event_status == "大涨":
                # 若当前大涨仍处在上一次严格金叉与下一次严格死叉之间，则直接按金叉处理。
                if strict_state == "金叉":
                    current_state = "金叉"
                    pending_dead_idx = None
                else:
                    current_state = "大涨"
                    pending_dead_idx = min(idx + self.oamv_big_rise_window - 1, len(normalized_dates) - 1)
            elif pending_dead_idx is not None and idx >= pending_dead_idx:
                current_state = "死叉"
                pending_dead_idx = None

            regimes.append(current_state)

        return pd.Series(regimes, index=trade_index, dtype="object")

    def _get_oamv_regime(self, trade_date: pd.Timestamp) -> str:
        if not hasattr(self, "_oamv_regime"):
            return "死叉"
        return str(self._oamv_regime.get(pd.Timestamp(trade_date), "死叉"))

    def _add_warning(self, ts_code: str, trade_date: pd.Timestamp, message: str) -> None:
        stock_data = self._stock_groups.get(ts_code)
        name = ts_code
        if stock_data is not None and not stock_data.empty and "name" in stock_data.columns:
            matched = stock_data.loc[stock_data["trade_date"] <= trade_date]
            if not matched.empty:
                name = str(matched.iloc[-1].get("name") or ts_code)
        key = f"{ts_code}|{pd.Timestamp(trade_date).strftime('%Y-%m-%d')}|{message}"
        if key in self._warning_keys:
            return
        self._warning_keys.add(key)
        self._warnings.append(
            f"{pd.Timestamp(trade_date).strftime('%Y-%m-%d')} {ts_code} {name}: {message}"
        )

    def _warn_chips_fetch_issue(self, ts_code: str, trade_date: pd.Timestamp, message: str) -> None:
        self._add_warning(ts_code, trade_date, message)

    def _warn_near_take_profit(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        ratio: float | None,
        label: str,
        threshold: float,
    ) -> None:
        if ratio is None:
            return
        lower_bound = threshold - self.take_profit_near_threshold_gap
        if lower_bound <= float(ratio) <= threshold:
            self._add_warning(
                ts_code,
                trade_date,
                f"接近止盈但未触发：{label}获利筹码占比 {float(ratio):.2f}% ，未超过阈值 {threshold:.2f}%",
            )

    def _load_local_chips_fallback(self) -> dict[tuple[str, pd.Timestamp], float]:
        path = self.chips_fallback_file
        if not path.exists():
            return {}

        frame = pd.read_excel(path, usecols=[0, 2, 3])
        if frame.empty:
            return {}

        frame = frame.rename(
            columns={
                frame.columns[0]: "ts_code",
                frame.columns[1]: "trade_date",
                frame.columns[2]: "profit_ratio",
            }
        )
        frame["ts_code"] = frame["ts_code"].astype(str).str.strip()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
        frame["profit_ratio"] = pd.to_numeric(frame["profit_ratio"], errors="coerce")
        frame = frame.dropna(subset=["ts_code", "trade_date", "profit_ratio"])

        fallback_map: dict[tuple[str, pd.Timestamp], float] = {}
        for row in frame.itertuples(index=False):
            ratio = float(row.profit_ratio)
            # Excel 中若使用 0~1 小数表示比例，则转换为 0~100 口径。
            if ratio <= 1.5:
                ratio *= 100.0
            fallback_map[(str(row.ts_code), pd.Timestamp(row.trade_date))] = ratio
        return fallback_map

    def _get_fallback_profit_ratio(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
    ) -> float | None:
        if not hasattr(self, "_chips_fallback_map") or not self._chips_fallback_map:
            return None
        return self._chips_fallback_map.get((ts_code, pd.Timestamp(trade_date).normalize()))

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
