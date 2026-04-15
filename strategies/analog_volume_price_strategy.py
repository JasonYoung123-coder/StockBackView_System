from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "量价模板相似策略"
    description = "基于四段样本行情学习量价模板，相似度选股并结合趋势风控交易。"
    is_portfolio_strategy = True
    lookback_days = 140

    sample_periods = (
        ("600366.SH", "宁波韵升", "2025-04-28", "2025-09-04"),
        ("688799.SH", "华纳药厂", "2025-04-08", "2025-06-30"),
        ("000547.SZ", "航天发展", "2025-10-17", "2026-01-21"),
        ("688516.SH", "奥特维", "2025-12-05", "2026-02-25"),
    )

    similarity_window = 12
    max_holdings = 2
    rebalance_interval = 5
    stop_loss = -0.08
    time_stop = 18
    trailing_take_profit = -0.11
    market_exit_gain_floor = 0.03
    min_circulating_cap = 20.0

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        if market_data.empty or not trade_dates:
            return pd.DataFrame(), {}

        prepared = self._prepare_market_data(market_data)
        templates = self._build_similarity_templates(prepared)
        prepared = self._add_pattern_similarity(prepared, templates)

        trade_index = pd.DatetimeIndex(trade_dates)
        all_codes = sorted(prepared["ts_code"].dropna().unique().tolist())
        weights = pd.DataFrame(0.0, index=trade_index, columns=all_codes, dtype=float)
        market_regime = self._build_market_regime(prepared, trade_index)
        day_frames = {
            trade_date: frame.set_index("ts_code", drop=False)
            for trade_date, frame in prepared.groupby("trade_date", sort=False)
        }
        date_to_index = {trade_date: idx for idx, trade_date in enumerate(trade_index)}
        rebalance_dates = set(trade_index[:: self.rebalance_interval])

        holdings: dict[str, dict] = {}
        latest_holdings: list[dict] = []
        sell_reasons: dict[str, str] = {}
        rebalance_count = 0

        for trade_date in trade_index:
            day_frame = day_frames.get(trade_date)
            if day_frame is None or day_frame.empty:
                continue

            regime = market_regime.loc[trade_date]
            target_exposure = float(regime["target_exposure"])
            target_count = self._target_holding_count(target_exposure)

            self._process_sells(
                holdings=holdings,
                day_frame=day_frame,
                trade_date=trade_date,
                trade_index=date_to_index,
                target_exposure=target_exposure,
                sell_reasons=sell_reasons,
            )

            if target_count <= 0:
                for ts_code in list(holdings.keys()):
                    snapshot = day_frame.loc[ts_code] if ts_code in day_frame.index else None
                    if snapshot is None:
                        holdings.pop(ts_code, None)
                        continue
                    current_return = float(snapshot["close"] / holdings[ts_code]["buy_price"] - 1.0)
                    if current_return < self.market_exit_gain_floor:
                        sell_reasons[f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = "市场转弱"
                        holdings.pop(ts_code, None)
            else:
                if trade_date in rebalance_dates and holdings:
                    ranked = self._rank_candidates(day_frame)
                    keep_codes = set(ranked.head(max(target_count, self.max_holdings))["ts_code"].tolist())
                    for ts_code in list(holdings.keys()):
                        if ts_code not in day_frame.index:
                            holdings.pop(ts_code, None)
                            continue
                        snapshot = day_frame.loc[ts_code]
                        current_return = float(snapshot["close"] / holdings[ts_code]["buy_price"] - 1.0)
                        weak_shape = float(snapshot["pattern_similarity"]) < 0.22
                        if ts_code not in keep_codes and current_return < 0.05 and weak_shape:
                            sell_reasons[f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = "调仓换股"
                            holdings.pop(ts_code, None)

                added = self._fill_positions(
                    holdings=holdings,
                    day_frame=day_frame,
                    trade_date=trade_date,
                    buy_index=date_to_index[trade_date],
                    target_count=target_count,
                )
                if added > 0:
                    rebalance_count += 1

            self._apply_weights(
                weights=weights,
                holdings=holdings,
                trade_date=trade_date,
                target_exposure=target_exposure,
            )
            latest_holdings = self._build_latest_holdings(holdings)

        return weights, {
            "rebalance_count": rebalance_count,
            "latest_holdings": latest_holdings,
            "sell_reasons": sell_reasons,
            "template_count": len(templates),
        }

    def _prepare_market_data(self, market_data: pd.DataFrame) -> pd.DataFrame:
        frame = market_data.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        frame["name"] = frame["name"].fillna("")
        frame["industry"] = frame["industry"].fillna("未知")

        for column in ("open", "high", "low", "close", "vol", "amount", "circ_mv"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        block_words = ["ST", "*ST", "S*ST", "SST", "退", "N ", "C "]
        for word in block_words:
            frame = frame[~frame["name"].str.contains(word, na=False, regex=False)]

        frame = frame.dropna(subset=["open", "high", "low", "close", "vol", "amount", "circ_mv"])
        frame = frame.loc[(frame["close"] > 0) & (frame["vol"] > 0) & (frame["circ_mv"] > 0)].copy()

        grouped = frame.groupby("ts_code", sort=False)
        frame["daily_return"] = grouped["close"].pct_change().fillna(0.0)
        frame["ret3"] = grouped["close"].pct_change(3)
        frame["ret5"] = grouped["close"].pct_change(5)
        frame["ret10"] = grouped["close"].pct_change(10)
        frame["ret20"] = grouped["close"].pct_change(20)
        frame["ma5"] = grouped["close"].transform(lambda s: s.rolling(5).mean())
        frame["ma10"] = grouped["close"].transform(lambda s: s.rolling(10).mean())
        frame["ma20"] = grouped["close"].transform(lambda s: s.rolling(20).mean())
        frame["ma60"] = grouped["close"].transform(lambda s: s.rolling(60).mean())
        frame["prev_high20"] = grouped["high"].transform(lambda s: s.rolling(20).max().shift(1))
        frame["prev_high60"] = grouped["high"].transform(lambda s: s.rolling(60).max().shift(1))
        frame["low20"] = grouped["low"].transform(lambda s: s.rolling(20).min())
        frame["vol_ma5"] = grouped["vol"].transform(lambda s: s.rolling(5).mean())
        frame["vol_ma20"] = grouped["vol"].transform(lambda s: s.rolling(20).mean())
        frame["amount_ma20"] = grouped["amount"].transform(lambda s: s.rolling(20).mean())
        frame["volatility10"] = grouped["daily_return"].transform(lambda s: s.rolling(10).std())
        frame["volatility20"] = grouped["daily_return"].transform(lambda s: s.rolling(20).std())
        frame["volatility40"] = grouped["daily_return"].transform(lambda s: s.rolling(40).std())

        frame["volume_ratio"] = frame["vol_ma5"] / frame["vol_ma20"]
        frame["breakout20"] = frame["close"] / frame["prev_high20"] - 1.0
        frame["breakout60"] = frame["close"] / frame["prev_high60"] - 1.0
        frame["drawdown20"] = frame["close"] / grouped["high"].transform(lambda s: s.rolling(20).max()) - 1.0
        frame["distance_ma20"] = frame["close"] / frame["ma20"] - 1.0
        frame["distance_high60"] = frame["close"] / frame["prev_high60"] - 1.0
        frame["rebound_from_low20"] = frame["close"] / frame["low20"] - 1.0
        frame["trend_repair"] = frame["ma10"] / frame["ma20"] - 1.0
        frame["trend_strength"] = frame["ma20"] / frame["ma60"] - 1.0
        frame["contraction_ratio"] = frame["volatility10"] / frame["volatility40"]
        frame["ma_alignment"] = (
            (frame["ma5"] > frame["ma10"]).astype(int)
            + (frame["ma10"] > frame["ma20"]).astype(int)
            + (frame["ma20"] > frame["ma60"]).astype(int)
        )
        return frame.reset_index(drop=True)

    def _build_similarity_templates(self, prepared: pd.DataFrame) -> list[dict]:
        templates: list[dict] = []
        for ts_code, name, start_date, end_date in self.sample_periods:
            sample = prepared.loc[
                (prepared["ts_code"] == ts_code)
                & (prepared["trade_date"] >= pd.Timestamp(start_date))
                & (prepared["trade_date"] <= pd.Timestamp(end_date))
            ].copy()
            sample = sample.reset_index(drop=True)
            if len(sample) < self.similarity_window:
                continue

            anchor = self._choose_template_anchor(sample)
            start_idx = max(0, anchor - self.similarity_window + 1)
            window = sample.iloc[start_idx : anchor + 1].copy()
            if len(window) < self.similarity_window:
                window = sample.iloc[: self.similarity_window].copy()
            if len(window) < self.similarity_window:
                continue

            templates.append(
                {
                    "name": name,
                    "price_signal": self._normalize_array(
                        window["daily_return"].fillna(0.0).to_numpy(dtype=float)
                    ),
                    "volume_signal": self._normalize_array(
                        np.log(
                            window["volume_ratio"]
                            .replace([np.inf, -np.inf], np.nan)
                            .fillna(1.0)
                            .clip(lower=0.2)
                            .to_numpy(dtype=float)
                        )
                    ),
                    "breakout_signal": self._normalize_array(
                        window["breakout20"]
                        .replace([np.inf, -np.inf], np.nan)
                        .fillna(-0.1)
                        .clip(lower=-0.3, upper=0.3)
                        .to_numpy(dtype=float)
                    ),
                }
            )
        return templates

    def _choose_template_anchor(self, sample: pd.DataFrame) -> int:
        early = sample.head(min(len(sample), 30)).copy()
        trigger = early.loc[
            (early["breakout20"] > -0.02)
            & (early["volume_ratio"] > 1.0)
            & (early["ret5"] > 0.02)
        ]
        if not trigger.empty:
            return int(trigger.index[0])

        fallback_score = (
            early["ret5"].fillna(-1.0) * 35.0
            + early["breakout20"].fillna(-1.0) * 50.0
            + early["volume_ratio"].fillna(0.0) * 2.5
        )
        return int(fallback_score.idxmax())

    def _add_pattern_similarity(
        self,
        prepared: pd.DataFrame,
        templates: list[dict],
    ) -> pd.DataFrame:
        frame = prepared.copy()
        frame["pattern_similarity"] = 0.0
        frame["best_template"] = ""

        if not templates:
            return frame

        for _, group in frame.groupby("ts_code", sort=False):
            if len(group) < self.similarity_window:
                continue

            daily_return = group["daily_return"].fillna(0.0).to_numpy(dtype=float)
            volume_signal = np.log(
                group["volume_ratio"]
                .replace([np.inf, -np.inf], np.nan)
                .fillna(1.0)
                .clip(lower=0.2)
                .to_numpy(dtype=float)
            )
            breakout_signal = (
                group["breakout20"]
                .replace([np.inf, -np.inf], np.nan)
                .fillna(-0.1)
                .clip(lower=-0.3, upper=0.3)
                .to_numpy(dtype=float)
            )

            price_windows = sliding_window_view(daily_return, self.similarity_window)
            volume_windows = sliding_window_view(volume_signal, self.similarity_window)
            breakout_windows = sliding_window_view(breakout_signal, self.similarity_window)

            norm_price = self._normalize_matrix(price_windows)
            norm_volume = self._normalize_matrix(volume_windows)
            norm_breakout = self._normalize_matrix(breakout_windows)

            best_similarity = np.full(price_windows.shape[0], -1.0, dtype=float)
            best_names = np.full(price_windows.shape[0], "", dtype=object)

            for template in templates:
                similarity = (
                    0.55 * (norm_price * template["price_signal"]).mean(axis=1)
                    + 0.25 * (norm_volume * template["volume_signal"]).mean(axis=1)
                    + 0.20 * (norm_breakout * template["breakout_signal"]).mean(axis=1)
                )
                improved = similarity > best_similarity
                best_similarity[improved] = similarity[improved]
                best_names[improved] = template["name"]

            target_index = group.index[self.similarity_window - 1 :]
            frame.loc[target_index, "pattern_similarity"] = best_similarity
            frame.loc[target_index, "best_template"] = best_names

        frame["pattern_similarity"] = frame["pattern_similarity"].clip(lower=-1.0, upper=1.0)
        return frame

    def _build_market_regime(
        self,
        prepared: pd.DataFrame,
        trade_index: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        breadth = (
            prepared.assign(
                above_ma20=prepared["close"] > prepared["ma20"],
                positive_ret5=prepared["ret5"] > 0,
                active_pattern=prepared["pattern_similarity"] > 0.22,
            )
            .groupby("trade_date")
            .agg(
                breadth_above_ma20=("above_ma20", "mean"),
                breadth_positive_ret5=("positive_ret5", "mean"),
                breadth_active_pattern=("active_pattern", "mean"),
                median_ret5=("ret5", "median"),
            )
            .reindex(trade_index)
            .ffill()
            .fillna(0.0)
        )

        exposure: list[float] = []
        for row in breadth.itertuples():
            if (
                row.breadth_above_ma20 >= 0.54
                and row.breadth_positive_ret5 >= 0.48
                and row.breadth_active_pattern >= 0.008
            ):
                exposure.append(1.0)
            elif row.breadth_above_ma20 >= 0.48 and row.breadth_positive_ret5 >= 0.43:
                exposure.append(0.65)
            elif row.breadth_above_ma20 >= 0.43 and row.median_ret5 >= -0.005:
                exposure.append(0.35)
            else:
                exposure.append(0.0)

        breadth["target_exposure"] = exposure
        return breadth

    def _rank_candidates(self, day_frame: pd.DataFrame) -> pd.DataFrame:
        frame = day_frame.copy()
        required_columns = [
            "pattern_similarity",
            "volume_ratio",
            "breakout20",
            "ret5",
            "ret20",
            "ma10",
            "ma20",
            "ma60",
            "distance_high60",
            "rebound_from_low20",
            "contraction_ratio",
            "volatility20",
            "amount_ma20",
            "circ_mv",
        ]
        frame = frame.dropna(subset=required_columns)
        if frame.empty:
            return pd.DataFrame(
                columns=["ts_code", "name", "industry", "score", "selection_reason", "best_template"]
            )

        eligible = frame.loc[
            (frame["circ_mv"] >= self.min_circulating_cap)
            & (frame["amount_ma20"] > 80000)
            & (frame["close"] >= 5.0)
            & (frame["pattern_similarity"] >= 0.22)
            & (frame["close"] > frame["ma20"])
            & (frame["ma10"] >= frame["ma20"] * 0.98)
            & (frame["breakout20"] >= -0.03)
            & (frame["ret5"].between(0.02, 0.28))
            & (frame["ret20"].between(-0.06, 0.45))
            & (frame["volume_ratio"].between(0.85, 2.5))
            & (frame["distance_high60"] >= -0.28)
            & (frame["rebound_from_low20"].between(0.06, 0.50))
            & (frame["contraction_ratio"] <= 1.75)
            & (frame["volatility20"] <= 0.085)
        ].copy()
        if eligible.empty:
            return pd.DataFrame(
                columns=["ts_code", "name", "industry", "score", "selection_reason", "best_template"]
            )

        eligible["score"] = (
            eligible["pattern_similarity"] * 130.0
            + eligible["breakout20"] * 70.0
            + eligible["ret5"] * 28.0
            + eligible["ret20"] * 14.0
            + eligible["volume_ratio"] * 3.5
            + eligible["trend_repair"].fillna(0.0) * 80.0
            - eligible["volatility20"] * 70.0
            - eligible["distance_high60"].abs() * 10.0
        )
        eligible["selection_reason"] = eligible["best_template"].map(
            lambda value: f"量价形态接近样本: {value}" if value else "量价模板相似"
        )
        return eligible.sort_values("score", ascending=False).reset_index(drop=True)

    def _process_sells(
        self,
        holdings: dict[str, dict],
        day_frame: pd.DataFrame,
        trade_date: pd.Timestamp,
        trade_index: dict[pd.Timestamp, int],
        target_exposure: float,
        sell_reasons: dict[str, str],
    ) -> None:
        for ts_code in list(holdings.keys()):
            if ts_code not in day_frame.index:
                holdings.pop(ts_code, None)
                continue

            snapshot = day_frame.loc[ts_code]
            holding = holdings[ts_code]
            current_price = float(snapshot["close"])
            current_return = current_price / float(holding["buy_price"]) - 1.0
            holding["peak_price"] = max(float(holding["peak_price"]), current_price)
            peak_drawdown = current_price / float(holding["peak_price"]) - 1.0
            holding_days = trade_index[trade_date] - int(holding["buy_index"])

            should_sell = False
            sell_reason = ""
            if current_return <= self.stop_loss:
                should_sell = True
                sell_reason = "固定止损"
            elif holding_days >= 5 and current_return <= -0.05:
                should_sell = True
                sell_reason = "弱势失败"
            elif current_return > 0.15 and peak_drawdown <= self.trailing_take_profit:
                should_sell = True
                sell_reason = "移动止盈"
            elif current_price < float(snapshot["ma10"]) and float(snapshot["ret5"]) < -0.03:
                should_sell = True
                sell_reason = "跌破短线趋势"
            elif holding_days >= self.time_stop and current_return < 0.12:
                should_sell = True
                sell_reason = "超时换仓"
            elif target_exposure <= 0.0 and current_return < self.market_exit_gain_floor:
                should_sell = True
                sell_reason = "市场转弱"
            elif float(snapshot["pattern_similarity"]) < 0.02 and current_price < float(snapshot["ma20"]):
                should_sell = True
                sell_reason = "形态失效"

            if should_sell:
                sell_reasons[f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = sell_reason
                holdings.pop(ts_code, None)

    def _fill_positions(
        self,
        holdings: dict[str, dict],
        day_frame: pd.DataFrame,
        trade_date: pd.Timestamp,
        buy_index: int,
        target_count: int,
    ) -> int:
        if len(holdings) >= target_count:
            return 0

        ranked = self._rank_candidates(day_frame)
        if ranked.empty:
            return 0

        industry_count: dict[str, int] = {}
        for item in holdings.values():
            industry = str(item["industry"])
            industry_count[industry] = industry_count.get(industry, 0) + 1

        added = 0
        for row in ranked.itertuples():
            if row.ts_code in holdings:
                continue
            if industry_count.get(str(row.industry), 0) >= 1:
                continue

            holdings[row.ts_code] = {
                "ts_code": row.ts_code,
                "name": row.name,
                "industry": row.industry,
                "score": float(row.score),
                "selection_reason": row.selection_reason,
                "best_template": getattr(row, "best_template", ""),
                "buy_date": trade_date,
                "buy_index": buy_index,
                "buy_price": float(row.close),
                "peak_price": float(row.close),
            }
            industry = str(row.industry)
            industry_count[industry] = industry_count.get(industry, 0) + 1
            added += 1
            if len(holdings) >= target_count:
                break
        return added

    def _apply_weights(
        self,
        weights: pd.DataFrame,
        holdings: dict[str, dict],
        trade_date: pd.Timestamp,
        target_exposure: float,
    ) -> None:
        weights.loc[trade_date, :] = 0.0
        if not holdings or target_exposure <= 0.0:
            return

        weight_per_stock = target_exposure / len(holdings)
        for holding in holdings.values():
            weights.loc[trade_date, holding["ts_code"]] = weight_per_stock

    def _target_holding_count(self, target_exposure: float) -> int:
        if target_exposure >= 0.95:
            return 2
        if target_exposure >= 0.45:
            return 1
        return 0

    def _build_latest_holdings(self, holdings: dict[str, dict]) -> list[dict]:
        rows = sorted(holdings.values(), key=lambda item: (-item["score"], item["ts_code"]))
        return [
            {
                "ts_code": item["ts_code"],
                "name": item["name"],
                "industry": item["industry"],
                "score": round(float(item["score"]), 2),
                "selection_reason": item["selection_reason"],
                "best_template": item["best_template"],
            }
            for item in rows
        ]

    @staticmethod
    def _normalize_array(values: np.ndarray) -> np.ndarray:
        values = values.astype(float)
        std = float(values.std(ddof=0))
        if std <= 1e-8:
            return np.zeros_like(values, dtype=float)
        return (values - values.mean()) / std

    @staticmethod
    def _normalize_matrix(values: np.ndarray) -> np.ndarray:
        mean = values.mean(axis=1, keepdims=True)
        std = values.std(axis=1, keepdims=True)
        std = np.where(std <= 1e-8, 1.0, std)
        return (values - mean) / std
