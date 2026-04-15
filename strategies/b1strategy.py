from __future__ import annotations

import math

import pandas as pd

from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "b1strategy"
    description = "放量异动后缩量回踩，再博弈后续加速拉升的组合选股策略。"
    is_portfolio_strategy = True
    lookback_days = 120

    max_holdings = 5
    position_per_stock = 0.2
    weak_rise_threshold = 0.02
    sell_volume_expand_threshold = 0.10
    stop_loss_threshold = -0.10
    full_position_threshold = 0.04
    sell_only_threshold = -0.023

    burst_min_return = 0.09
    pullback_window_min = 2
    pullback_window_max = 8
    pullback_min_retracement = 0.18
    pullback_max_retracement = 0.55
    pullback_volume_ratio_max = 0.72
    rebound_min_return = 0.015
    kdj_j_threshold = 55
    price_diff_threshold = 0.05

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        frame = market_data.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        frame["prev_vol"] = frame.groupby("ts_code")["vol"].shift(1)

        universe = sorted(frame["ts_code"].dropna().unique())
        weights = pd.DataFrame(0.0, index=pd.DatetimeIndex(trade_dates), columns=universe)
        sell_reasons: dict[str, str] = {}
        holdings: dict[str, dict] = {}
        rebalance_count = 0
        market_regime = self._build_market_regime(frame, trade_dates)
        latest_holdings: list[dict] = []

        grouped = {ts_code: group.reset_index(drop=True) for ts_code, group in frame.groupby("ts_code", sort=False)}

        for index, trade_date in enumerate(trade_dates):
            progress = 55 + (index + 1) / max(len(trade_dates), 1) * 35
            self.report_progress(progress, f"b1strategy 选股与调仓 {index + 1}/{len(trade_dates)}")
            regime = market_regime.get(trade_date, {"full_position": False, "sell_only": False})
            tradable_weight = 1.0 if regime["full_position"] else self.max_holdings * self.position_per_stock
            tradable_weight = min(tradable_weight, 1.0)

            current_candidates: list[dict] = []
            for ts_code, position in list(holdings.items()):
                history = grouped.get(ts_code)
                if history is None:
                    continue
                row = history.loc[history["trade_date"] == trade_date]
                if row.empty:
                    continue
                decision = self._should_sell_position(position, row.iloc[-1])
                if decision is not None:
                    sell_reasons[f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = decision
                    holdings.pop(ts_code, None)

            if not regime["sell_only"]:
                scores: list[dict] = []
                for ts_code, history in grouped.items():
                    sliced = history.loc[history["trade_date"] <= trade_date].tail(self.lookback_days).copy()
                    if len(sliced) < 40:
                        continue
                    score = self._score_history(sliced)
                    if score is None:
                        continue
                    scores.append(score)

                scores.sort(key=lambda item: item["score"], reverse=True)
                current_candidates = scores[: self.max_holdings]
                for item in current_candidates:
                    if item["ts_code"] not in holdings and len(holdings) < self.max_holdings:
                        holdings[item["ts_code"]] = {
                            "buy_date": trade_date,
                            "buy_price": float(item["close"]),
                            "score": item["score"],
                            "name": item["name"],
                        }
                        rebalance_count += 1

            latest_holdings = [
                {
                    "ts_code": ts_code,
                    "name": data.get("name", ts_code),
                    "score": round(float(data.get("score", 0.0)), 4),
                }
                for ts_code, data in holdings.items()
            ]

            if holdings:
                per_weight = min(tradable_weight / len(holdings), self.position_per_stock if not regime["full_position"] else 1.0 / len(holdings))
                for ts_code in holdings:
                    weights.loc[trade_date, ts_code] = per_weight

        return weights, {
            "rebalance_count": rebalance_count,
            "latest_holdings": latest_holdings,
            "sell_reasons": sell_reasons,
        }

    def _build_market_regime(self, market_data: pd.DataFrame, trade_dates: list[pd.Timestamp]) -> dict[pd.Timestamp, dict[str, bool]]:
        market = market_data.groupby("trade_date", as_index=False)["circ_mv"].sum(min_count=1)
        market["circ_mv"] = pd.to_numeric(market["circ_mv"], errors="coerce").fillna(method="ffill")
        market["daily_change"] = market["circ_mv"].pct_change().replace([math.inf, -math.inf], pd.NA).fillna(0.0)
        market["three_day_change"] = market["circ_mv"].pct_change(3).replace([math.inf, -math.inf], pd.NA).fillna(0.0)
        regime_map: dict[pd.Timestamp, dict[str, bool]] = {}
        for trade_date in trade_dates:
            row = market.loc[market["trade_date"] == trade_date]
            if row.empty:
                regime_map[trade_date] = {"full_position": False, "sell_only": False}
                continue
            last = row.iloc[-1]
            regime_map[trade_date] = {
                "full_position": bool(last["three_day_change"] >= self.full_position_threshold),
                "sell_only": bool(last["daily_change"] < self.sell_only_threshold),
            }
        return regime_map

    def _should_sell_position(self, position: dict, row: pd.Series) -> str | None:
        close_price = float(row.get("close", 0.0) or 0.0)
        open_price = float(row.get("open", 0.0) or 0.0)
        volume = float(row.get("vol", 0.0) or 0.0)
        prev_close = float(row.get("pre_close", 0.0) or 0.0)
        prev_volume = float(row.get("prev_vol", row.get("vol_ma5", 0.0)) or 0.0)

        if close_price <= 0 or position["buy_price"] <= 0:
            return None

        return_rate = close_price / float(position["buy_price"]) - 1.0
        if return_rate <= self.stop_loss_threshold:
            return "止损"

        day_return = close_price / prev_close - 1.0 if prev_close > 0 else 0.0
        volume_ratio = volume / prev_volume - 1.0 if prev_volume > 0 else 0.0
        if (
            day_return < self.weak_rise_threshold
            and close_price < open_price
            and volume_ratio > self.sell_volume_expand_threshold
        ):
            return "高位放量转弱"
        return None

    def _score_history(self, history: pd.DataFrame) -> dict | None:
        series = history.copy()
        series["close"] = pd.to_numeric(series["close"], errors="coerce")
        series["open"] = pd.to_numeric(series["open"], errors="coerce")
        series["high"] = pd.to_numeric(series["high"], errors="coerce")
        series["low"] = pd.to_numeric(series["low"], errors="coerce")
        series["vol"] = pd.to_numeric(series["vol"], errors="coerce")
        series["pct_change"] = series["close"].pct_change().replace([math.inf, -math.inf], pd.NA).fillna(0.0)
        series["vol_ma5"] = series["vol"].rolling(5).mean()
        series["vol_ma10"] = series["vol"].rolling(10).mean()
        series["ma10"] = series["close"].rolling(10).mean()
        series["ma20"] = series["close"].rolling(20).mean()
        series["ma30"] = series["close"].rolling(30).mean()

        if len(series) < 35 or series[["close", "vol"]].tail(20).isna().any().any():
            return None

        setup = self._evaluate_burst_pullback_setup(series)
        if setup is None:
            return None

        latest = series.iloc[-1]
        k_value, d_value, j_value = self._calculate_kdj(series["high"], series["low"], series["close"])
        if pd.isna(j_value) or j_value > self.kdj_j_threshold:
            return None

        ma10 = float(latest["ma10"])
        ma20 = float(latest["ma20"])
        ma30 = float(latest["ma30"])
        if any(pd.isna(value) for value in (ma10, ma20, ma30)):
            return None

        ma_distance = abs(ma10 / ma20 - 1.0)
        if ma10 < ma20 or ma20 < ma30 or ma_distance > self.price_diff_threshold:
            return None

        score = (
            setup["score"]
            + max(0.0, (self.kdj_j_threshold - float(j_value)) * 0.08)
            + max(0.0, (ma20 / ma30 - 1.0) * 100)
        )

        return {
            "ts_code": latest["ts_code"],
            "name": str(latest.get("name") or latest["ts_code"]),
            "close": float(latest["close"]),
            "score": round(float(score), 4),
            "selection_reason": "放量异动后缩量回踩",
        }

    def _evaluate_burst_pullback_setup(self, history: pd.DataFrame) -> dict | None:
        latest = history.iloc[-1]
        closes = history["close"].reset_index(drop=True)
        volumes = history["vol"].reset_index(drop=True)
        pct_changes = history["pct_change"].reset_index(drop=True)
        latest_index = len(history) - 1

        for pullback_days in range(self.pullback_window_min, self.pullback_window_max + 1):
            burst_index = latest_index - pullback_days
            if burst_index < 12:
                continue
            burst_return = float(pct_changes.iloc[burst_index])
            burst_volume = float(volumes.iloc[burst_index])
            burst_close = float(closes.iloc[burst_index])
            if burst_return < self.burst_min_return:
                continue

            pullback_slice = history.iloc[burst_index + 1 : latest_index + 1].copy()
            if pullback_slice.empty:
                continue

            low_price = float(pullback_slice["low"].min())
            retracement = (burst_close - low_price) / max(burst_close, 1e-9)
            avg_pullback_volume = float(pullback_slice["vol"].mean())
            volume_ratio = avg_pullback_volume / max(burst_volume, 1e-9)
            rebound_return = float(latest["close"] / closes.iloc[max(burst_index + 1, 0)] - 1.0)

            if not (self.pullback_min_retracement <= retracement <= self.pullback_max_retracement):
                continue
            if volume_ratio > self.pullback_volume_ratio_max:
                continue
            if rebound_return < self.rebound_min_return:
                continue

            score = (
                burst_return * 120
                + (1.0 - volume_ratio) * 30
                + rebound_return * 100
                - abs(retracement - 0.3) * 18
            )
            return {
                "score": score,
                "burst_return": burst_return,
                "retracement": retracement,
                "volume_ratio": volume_ratio,
                "rebound_return": rebound_return,
            }
        return None

    def _calculate_kdj(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 9):
        lowest_low = low.rolling(period).min()
        highest_high = high.rolling(period).max()
        rsv = (close - lowest_low) / (highest_high - lowest_low).replace(0, pd.NA) * 100
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        j = 3 * k - 2 * d
        return k.iloc[-1], d.iloc[-1], j.iloc[-1]
