from __future__ import annotations

import pandas as pd

from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "高胜率低回撤趋势轮动策略"
    description = "基于市场宽度风控、趋势筛选和严格止损的低回撤组合策略。"
    is_portfolio_strategy = True
    lookback_days = 120

    max_holdings = 3
    rebalance_interval = 7
    hard_stop_loss = -0.045
    trailing_stop_drawdown = -0.05
    take_profit = 0.14
    max_holding_days = 15
    min_circulating_cap = 50.0

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        if market_data.empty or not trade_dates:
            return pd.DataFrame(), {}

        prepared = self._prepare_market_data(market_data)
        trade_index = pd.DatetimeIndex(trade_dates)
        all_codes = sorted(prepared["ts_code"].dropna().unique().tolist())
        weights = pd.DataFrame(0.0, index=trade_index, columns=all_codes, dtype=float)
        market_regime = self._build_market_regime(prepared, trade_index)
        day_frames = {
            trade_date: frame.set_index("ts_code", drop=False)
            for trade_date, frame in prepared.groupby("trade_date", sort=False)
        }
        date_to_index = {trade_date: idx for idx, trade_date in enumerate(trade_index)}
        scheduled_rebalance_dates = set(trade_index[:: self.rebalance_interval])

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
            is_rebalance_day = trade_date in scheduled_rebalance_dates

            self._process_sells(
                holdings=holdings,
                day_frame=day_frame,
                trade_date=trade_date,
                trade_index=date_to_index,
                target_exposure=target_exposure,
                sell_reasons=sell_reasons,
            )

            if is_rebalance_day and holdings:
                ranked = self._rank_candidates(day_frame)
                keep_codes = set(ranked.head(max(target_count, self.max_holdings))["ts_code"].tolist())
                for ts_code in list(holdings.keys()):
                    snapshot = day_frame.loc[ts_code] if ts_code in day_frame.index else None
                    if snapshot is None:
                        holdings.pop(ts_code, None)
                        continue
                    current_return = float(snapshot["close"] / holdings[ts_code]["buy_price"] - 1.0)
                    if ts_code not in keep_codes and current_return < -0.03:
                        sell_reasons[f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = "调仓换股"
                        holdings.pop(ts_code, None)

            if target_count > 0:
                added = self._fill_positions(
                    holdings=holdings,
                    day_frame=day_frame,
                    trade_date=trade_date,
                    buy_index=date_to_index[trade_date],
                    target_count=target_count,
                )
                if added > 0 and (is_rebalance_day or target_exposure > 0):
                    rebalance_count += 1
            else:
                if holdings:
                    for ts_code in list(holdings.keys()):
                        sell_reasons[f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = "市场转弱空仓"
                    holdings.clear()

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
            "market_regime": market_regime.reset_index().rename(columns={"index": "trade_date"}).to_dict("records"),
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
        frame["ret5"] = grouped["close"].pct_change(5)
        frame["ret10"] = grouped["close"].pct_change(10)
        frame["ret20"] = grouped["close"].pct_change(20)
        frame["ret60"] = grouped["close"].pct_change(60)
        frame["ma10"] = grouped["close"].transform(lambda s: s.rolling(10).mean())
        frame["ma20"] = grouped["close"].transform(lambda s: s.rolling(20).mean())
        frame["ma60"] = grouped["close"].transform(lambda s: s.rolling(60).mean())
        frame["high20"] = grouped["high"].transform(lambda s: s.rolling(20).max())
        frame["high60"] = grouped["high"].transform(lambda s: s.rolling(60).max())
        frame["vol_ma5"] = grouped["vol"].transform(lambda s: s.rolling(5).mean())
        frame["vol_ma20"] = grouped["vol"].transform(lambda s: s.rolling(20).mean())
        frame["amount_ma20"] = grouped["amount"].transform(lambda s: s.rolling(20).mean())
        frame["volatility20"] = grouped["daily_return"].transform(lambda s: s.rolling(20).std())
        frame["ma20_slope5"] = grouped["close"].transform(
            lambda s: s.rolling(20).mean() / s.rolling(20).mean().shift(5) - 1.0
        )
        frame["drawdown_from_high20"] = frame["close"] / frame["high20"] - 1.0
        frame["distance_to_high60"] = frame["close"] / frame["high60"] - 1.0
        frame["volume_ratio"] = frame["vol_ma5"] / frame["vol_ma20"]
        return frame.reset_index(drop=True)

    def _build_market_regime(
        self,
        prepared: pd.DataFrame,
        trade_index: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        breadth = (
            prepared.assign(
                above_ma20=prepared["close"] > prepared["ma20"],
                above_ma60=prepared["close"] > prepared["ma60"],
                positive_ret20=prepared["ret20"] > 0,
            )
            .groupby("trade_date")
            .agg(
                breadth_above_ma20=("above_ma20", "mean"),
                breadth_above_ma60=("above_ma60", "mean"),
                breadth_positive_ret20=("positive_ret20", "mean"),
                median_ret5=("ret5", "median"),
                median_ret20=("ret20", "median"),
            )
            .reindex(trade_index)
            .ffill()
            .fillna(0.0)
        )

        exposure = []
        for row in breadth.itertuples():
            if (
                row.breadth_above_ma20 >= 0.63
                and row.breadth_above_ma60 >= 0.55
                and row.breadth_positive_ret20 >= 0.60
                and row.median_ret20 >= 0.02
            ):
                exposure.append(0.90)
            elif (
                row.breadth_above_ma20 >= 0.58
                and row.breadth_above_ma60 >= 0.48
                and row.breadth_positive_ret20 >= 0.55
                and row.median_ret20 >= 0.005
            ):
                exposure.append(0.45)
            else:
                exposure.append(0.0)

        breadth["target_exposure"] = exposure
        breadth["sell_only"] = (breadth["target_exposure"] <= 0.0) | (breadth["median_ret5"] < -0.02)
        return breadth

    def _rank_candidates(self, day_frame: pd.DataFrame) -> pd.DataFrame:
        frame = day_frame.copy()
        required_columns = [
            "ret5",
            "ret20",
            "ret60",
            "ma10",
            "ma20",
            "ma60",
            "high20",
            "high60",
            "volatility20",
            "volume_ratio",
            "ma20_slope5",
            "drawdown_from_high20",
            "distance_to_high60",
            "amount_ma20",
            "circ_mv",
        ]
        frame = frame.dropna(subset=required_columns)
        if frame.empty:
            return pd.DataFrame(columns=["ts_code", "name", "industry", "score", "selection_reason", "current_price"])

        eligible = frame.loc[
            (frame["circ_mv"] >= self.min_circulating_cap)
            & (frame["close"] >= 6.0)
            & (frame["close"] > frame["ma20"])
            & (frame["ma20"] > frame["ma60"])
            & (frame["close"] > frame["ma10"])
            & (frame["ret20"].between(0.08, 0.20))
            & (frame["ret60"].between(0.15, 0.45))
            & (frame["ret5"].between(0.01, 0.08))
            & (frame["ma20_slope5"] > 0.015)
            & (frame["drawdown_from_high20"] >= -0.04)
            & (frame["distance_to_high60"] >= -0.06)
            & (frame["volatility20"] <= 0.03)
            & (frame["volume_ratio"].between(1.0, 1.8))
            & (frame["amount_ma20"] > 0)
        ].copy()
        if eligible.empty:
            return pd.DataFrame(columns=["ts_code", "name", "industry", "score", "selection_reason", "current_price"])

        eligible["score"] = (
            eligible["ret20"] * 120.0
            + eligible["ret60"] * 90.0
            + eligible["ma20_slope5"] * 120.0
            + eligible["distance_to_high60"] * 40.0
            + eligible["volume_ratio"] * 4.0
            - eligible["volatility20"] * 120.0
            + eligible["drawdown_from_high20"] * 30.0
        )
        eligible["selection_reason"] = "市场转强后的高质量趋势延续"
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
            holding["peak_price"] = max(float(holding["peak_price"]), current_price)
            current_return = current_price / float(holding["buy_price"]) - 1.0
            peak_drawdown = current_price / float(holding["peak_price"]) - 1.0
            holding_days = trade_index[trade_date] - int(holding["buy_index"])

            should_sell = False
            sell_reason = ""
            if current_return <= self.hard_stop_loss:
                should_sell = True
                sell_reason = "固定止损"
            elif peak_drawdown <= self.trailing_stop_drawdown and current_return > 0.05:
                should_sell = True
                sell_reason = "移动止盈"
            elif current_return >= self.take_profit and float(snapshot["ret5"]) < 0:
                should_sell = True
                sell_reason = "止盈落袋"
            elif current_price < float(snapshot["ma10"]) and current_return < -0.02:
                should_sell = True
                sell_reason = "跌破短线均线"
            elif holding_days >= self.max_holding_days and current_return < 0.08:
                should_sell = True
                sell_reason = "超时换仓"
            elif target_exposure <= 0.0 and current_price < float(snapshot["ma20"]):
                should_sell = True
                sell_reason = "市场转弱"

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
                "buy_date": trade_date,
                "buy_index": buy_index,
                "buy_price": float(row.close),
                "peak_price": float(row.close),
            }
            added += 1
            industry = str(row.industry)
            industry_count[industry] = industry_count.get(industry, 0) + 1
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
        return 1 if target_exposure > 0 else 0

    def _build_latest_holdings(self, holdings: dict[str, dict]) -> list[dict]:
        rows = sorted(holdings.values(), key=lambda item: (-item["score"], item["ts_code"]))
        return [
            {
                "ts_code": item["ts_code"],
                "name": item["name"],
                "industry": item["industry"],
                "score": round(float(item["score"]), 2),
                "selection_reason": item["selection_reason"],
            }
            for item in rows
        ]
