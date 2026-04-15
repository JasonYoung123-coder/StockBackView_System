from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd

from app.services.tushare_client import TushareClient
from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "attack_eff_v2进攻效率策略"
    description = "基于 attack_eff_v2_raw 公式打分，持有前4名正分股，次日开盘交易并结合筹码止盈/动量止损。"
    is_portfolio_strategy = True
    lookback_days = 40

    _CHIPS_CHUNK_TRADING_DAYS = 30
    _LIMIT_UP_SHRINK_RATIO = 0.5

    def __init__(
        self,
        max_holdings: int = 4,
        score_floor: float = 0.0,
        mf_weight_base: float = 0.60,
        vr_lookback: int = 5,
        take_profit_chip_threshold: float = 99.0,
    ) -> None:
        self.max_holdings = max(int(max_holdings), 1)
        self.score_floor = float(score_floor)
        self.mf_weight_base = float(mf_weight_base)
        self.vr_lookback = max(int(vr_lookback), 2)
        self.take_profit_chip_threshold = float(take_profit_chip_threshold)
        self.position_per_stock = 1.0 / self.max_holdings
        self._tushare = TushareClient()

    def get_config_schema(self) -> dict:
        return {
            "title": "attack_eff_v2 参数",
            "fields": [
                {
                    "name": "max_holdings",
                    "label": "最大持仓数",
                    "type": "number",
                    "min": 1,
                    "max": 10,
                    "step": 1,
                    "default": self.max_holdings,
                },
                {
                    "name": "score_floor",
                    "label": "最低分数门槛",
                    "type": "number",
                    "min": -10,
                    "max": 10,
                    "step": 0.01,
                    "default": self.score_floor,
                },
                {
                    "name": "mf_weight_base",
                    "label": "资金分位基础权重",
                    "type": "number",
                    "min": 0,
                    "max": 2,
                    "step": 0.01,
                    "default": self.mf_weight_base,
                },
                {
                    "name": "vr_lookback",
                    "label": "量比基准窗口",
                    "type": "number",
                    "min": 2,
                    "max": 20,
                    "step": 1,
                    "default": self.vr_lookback,
                },
                {
                    "name": "take_profit_chip_threshold",
                    "label": "止盈获利筹码阈值(%)",
                    "type": "number",
                    "min": 80,
                    "max": 100,
                    "step": 0.1,
                    "default": self.take_profit_chip_threshold,
                },
            ],
        }

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        if market_data.empty or not trade_dates:
            return pd.DataFrame(), {}

        prepared = self._prepare_market_data(market_data)
        if prepared.empty:
            return pd.DataFrame(), {}

        trade_index = pd.DatetimeIndex(pd.to_datetime(trade_dates))
        all_codes = sorted(prepared["ts_code"].dropna().unique().tolist())
        weights = pd.DataFrame(0.0, index=trade_index, columns=all_codes, dtype=float)

        self._trade_dates_list = list(trade_index)
        self._date_to_list_idx = {pd.Timestamp(day): idx for idx, day in enumerate(self._trade_dates_list)}
        self._stock_groups = {
            ts_code: group.sort_values("trade_date").set_index("trade_date", drop=False)
            for ts_code, group in prepared.groupby("ts_code", sort=False)
        }
        self._chips_cache: dict[str, pd.DataFrame] = {}
        self._chips_fetched_end: dict[str, str] = {}
        self._chips_missing_attempts: dict[str, set[str]] = {}

        day_frames = {
            trade_date: frame.set_index("ts_code", drop=False)
            for trade_date, frame in prepared.groupby("trade_date", sort=False)
        }

        holdings: dict[str, dict[str, object]] = {}
        sell_reasons: dict[str, str] = {}
        sell_orders: dict[str, dict[str, object]] = {}
        buy_prices: dict[str, float] = {}
        warnings: list[str] = []
        rebalance_count = 0

        total_days = len(trade_index)
        for i, trade_date in enumerate(trade_index):
            self.report_progress(
                60 + ((i + 1) / max(total_days, 1)) * 30,
                f"attack_eff_v2 调仓 {i + 1}/{total_days}",
            )
            day_frame = day_frames.get(trade_date)
            if day_frame is None or day_frame.empty:
                continue

            sold_today = self._process_sells(
                holdings=holdings,
                day_frame=day_frame,
                trade_date=trade_date,
                sell_reasons=sell_reasons,
                sell_orders=sell_orders,
                warnings=warnings,
            )

            signal_frame = day_frames.get(trade_index[i - 1]) if i >= 1 else None
            if signal_frame is not None and not signal_frame.empty:
                added = self._fill_positions(
                    holdings=holdings,
                    day_frame=signal_frame,
                    trade_date=trade_date,
                    buy_prices=buy_prices,
                    blocked_codes=sold_today,
                    warnings=warnings,
                )
            else:
                added = 0
            if sold_today or added > 0:
                rebalance_count += 1

            self._apply_weights(weights, holdings, trade_date)

        latest_holdings = self._build_latest_holdings(holdings)
        return weights, {
            "rebalance_count": rebalance_count,
            "latest_holdings": latest_holdings,
            "sell_reasons": sell_reasons,
            "sell_orders": sell_orders,
            "buy_prices": buy_prices,
            "warnings": warnings[:50],
            "chips_data_source_report": self._tushare.get_chips_data_source_report(),
        }

    def _prepare_market_data(self, market_data: pd.DataFrame) -> pd.DataFrame:
        frame = market_data.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        if "name" not in frame.columns:
            frame["name"] = ""
        else:
            frame["name"] = frame["name"].fillna("")
        if "industry" not in frame.columns:
            frame["industry"] = "未知"
        else:
            frame["industry"] = frame["industry"].fillna("未知")

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "pct_chg",
            "vol",
            "amount",
            "turnover_rate",
            "net_mf_amount",
            "close_unadj",
        ]
        for column in numeric_columns:
            if column not in frame.columns:
                frame[column] = np.nan
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["close_unadj"] = frame["close_unadj"].fillna(frame["close"])

        block_words = ["ST", "*ST", "S*ST", "SST", "退", "N ", "C "]
        for word in block_words:
            frame = frame.loc[~frame["name"].str.contains(word, na=False, regex=False)]

        frame = frame.dropna(
            subset=[
                "open",
                "close",
                "pre_close",
                "pct_chg",
                "vol",
                "amount",
                "turnover_rate",
                "net_mf_amount",
            ]
        ).copy()
        frame = frame.loc[
            (frame["open"] > 0)
            & (frame["close"] > 0)
            & (frame["pre_close"] > 0)
            & (frame["vol"] > 0)
            & (frame["amount"] > 0)
            & (frame["turnover_rate"] >= 0)
        ].copy()

        grouped = frame.groupby("ts_code", sort=False)
        frame["prev_pct_chg"] = grouped["pct_chg"].shift(1)
        frame["prev_vol"] = grouped["vol"].shift(1)
        frame["is_limit_up_close"] = frame.apply(
            lambda row: self._is_close_at_limit_up(str(row["ts_code"]), pd.Timestamp(row["trade_date"]), row),
            axis=1,
        )
        frame["non_limit_vol"] = frame["vol"].where(~frame["is_limit_up_close"], np.nan)
        frame["last_non_limit_vol"] = grouped["non_limit_vol"].transform(lambda series: series.ffill().shift(1))
        frame["is_limit_up_shrink_single"] = (
            frame["is_limit_up_close"]
            & frame["last_non_limit_vol"].notna()
            & (frame["vol"] < frame["last_non_limit_vol"] * self._LIMIT_UP_SHRINK_RATIO)
        )
        frame["prev_is_limit_up_shrink"] = grouped["is_limit_up_shrink_single"].shift(1).fillna(False)
        frame["is_limit_up_shrink"] = (
            frame["is_limit_up_shrink_single"] & frame["prev_is_limit_up_shrink"]
        )
        frame["vol_base"] = grouped["vol"].transform(
            lambda series: series.shift(1).rolling(self.vr_lookback).mean()
        )
        frame["vr"] = frame["vol"] / frame["vol_base"]
        frame["mf_strength"] = frame["net_mf_amount"] / frame["amount"]
        frame["mf_rank"] = frame.groupby("trade_date", group_keys=False)["mf_strength"].apply(
            self._to_percentile_rank
        )

        denominator = np.log1p(frame["vr"]) * np.sqrt(1.0 + frame["turnover_rate"])
        denominator = denominator.replace([np.inf, -np.inf], np.nan)
        frame["attack_eff_v2_raw"] = (
            frame["pct_chg"] * (self.mf_weight_base + frame["mf_rank"]) / denominator
        )
        frame["attack_eff_v2_raw"] = frame["attack_eff_v2_raw"].replace([np.inf, -np.inf], np.nan)
        return frame.reset_index(drop=True)

    @staticmethod
    def _to_percentile_rank(series: pd.Series) -> pd.Series:
        values = pd.to_numeric(series, errors="coerce")
        result = pd.Series(np.nan, index=series.index, dtype=float)
        valid = values.dropna()
        count = len(valid)
        if count == 0:
            return result
        if count == 1:
            result.loc[valid.index] = 1.0
            return result
        ranks = valid.rank(method="average", ascending=True)
        result.loc[valid.index] = (ranks - 1.0) / (count - 1.0)
        return result.clip(lower=0.0, upper=1.0)

    def _process_sells(
        self,
        holdings: dict[str, dict[str, object]],
        day_frame: pd.DataFrame,
        trade_date: pd.Timestamp,
        sell_reasons: dict[str, str],
        sell_orders: dict[str, dict[str, object]],
        warnings: list[str],
    ) -> set[str]:
        sold_codes: set[str] = set()
        for ts_code in list(holdings.keys()):
            if ts_code not in day_frame.index:
                warnings.append(f"{trade_date.strftime('%Y-%m-%d')} {ts_code} 缺少当日行情，已跳过卖出判断。")
                continue

            snapshot = day_frame.loc[ts_code]
            buy_date = pd.Timestamp(holdings[ts_code]["buy_date"])
            reason = ""

            if self._should_take_profit(ts_code, snapshot, trade_date, warnings):
                reason = "止盈-获利筹码占比超过99且放量"
            elif trade_date > buy_date and self._should_stop_loss(snapshot):
                reason = "止损-当日涨幅低于昨日涨幅"

            if not reason:
                continue

            execution = self._get_next_open_sell_execution(ts_code, trade_date)
            if execution is None:
                warnings.append(
                    f"{trade_date.strftime('%Y-%m-%d')} {ts_code} 触发卖出但缺少次日开盘价，回测中继续持有。"
                )
                continue

            execution_date, execution_price = execution
            signal_key = f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"
            sell_reasons[signal_key] = reason
            sell_orders[signal_key] = {
                "execution_date": execution_date.strftime("%Y-%m-%d"),
                "execution_price": execution_price,
            }
            holdings.pop(ts_code, None)
            sold_codes.add(ts_code)
        return sold_codes

    def _should_take_profit(
        self,
        ts_code: str,
        snapshot: pd.Series,
        trade_date: pd.Timestamp,
        warnings: list[str],
    ) -> bool:
        prev_vol = snapshot.get("prev_vol")
        current_vol = snapshot.get("vol")
        if pd.isna(prev_vol) or pd.isna(current_vol) or float(current_vol) <= float(prev_vol):
            return False

        self._ensure_chips_for_date(ts_code, trade_date, warnings)
        close_price = self._get_chip_reference_price(snapshot)
        ratio = self._get_profit_ratio(ts_code, trade_date, close_price)
        return ratio is not None and float(ratio) > self.take_profit_chip_threshold

    @staticmethod
    def _should_stop_loss(snapshot: pd.Series) -> bool:
        current_pct = snapshot.get("pct_chg")
        prev_pct = snapshot.get("prev_pct_chg")
        if pd.isna(current_pct) or pd.isna(prev_pct):
            return False
        return float(current_pct) < float(prev_pct)

    def _fill_positions(
        self,
        holdings: dict[str, dict[str, object]],
        day_frame: pd.DataFrame,
        trade_date: pd.Timestamp,
        buy_prices: dict[str, float],
        blocked_codes: set[str],
        warnings: list[str],
    ) -> int:
        available = self.max_holdings - len(holdings)
        if available <= 0:
            return 0

        ranked = self._rank_candidates(day_frame, holdings, blocked_codes)
        if ranked.empty:
            return 0

        added = 0
        for row in ranked.itertuples():
            if row.ts_code in holdings:
                continue

            execution = self._get_buy_execution(row.ts_code, trade_date)
            if execution is None:
                warnings.append(
                    f"{trade_date.strftime('%Y-%m-%d')} {row.ts_code} {row.name} 次日开盘即涨停或无有效开盘价，买入失败。"
                )
                continue

            execution_date, execution_price = execution
            holdings[row.ts_code] = {
                "ts_code": row.ts_code,
                "name": row.name,
                "industry": row.industry,
                "score": float(row.attack_eff_v2_raw),
                "selection_reason": (
                    f"score={float(row.attack_eff_v2_raw):.4f}, "
                    f"mf_rank={float(row.mf_rank):.4f}, "
                    f"vr={float(row.vr):.4f}, "
                    f"to={float(row.turnover_rate):.4f}"
                ),
                "buy_signal_date": trade_date,
                "buy_date": execution_date,
                "buy_price": execution_price,
                "weight": self.position_per_stock,
            }
            buy_prices[f"{row.ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = execution_price
            added += 1
            if added >= available:
                break
        return added

    def _rank_candidates(
        self,
        day_frame: pd.DataFrame,
        holdings: dict[str, dict[str, object]],
        blocked_codes: set[str],
    ) -> pd.DataFrame:
        frame = day_frame.reset_index(drop=True).copy()
        required_columns = [
            "attack_eff_v2_raw",
            "mf_rank",
            "vr",
            "turnover_rate",
            "pct_chg",
        ]
        frame = frame.dropna(subset=required_columns).copy()
        if frame.empty:
            return pd.DataFrame()

        frame = frame.loc[
            (frame["attack_eff_v2_raw"] > self.score_floor)
            & (frame["vr"] > 0)
            & (frame["turnover_rate"] >= 0)
            & (~frame["is_limit_up_shrink"].fillna(False))
            & (~frame["ts_code"].isin(blocked_codes))
            & (~frame["ts_code"].isin(holdings.keys()))
        ].copy()
        if frame.empty:
            return pd.DataFrame()

        return frame.sort_values(
            ["attack_eff_v2_raw", "mf_rank", "pct_chg", "ts_code"],
            ascending=[False, False, False, True],
        ).reset_index(drop=True)

    def _apply_weights(
        self,
        weights: pd.DataFrame,
        holdings: dict[str, dict[str, object]],
        trade_date: pd.Timestamp,
    ) -> None:
        weights.loc[trade_date, :] = 0.0
        if not holdings:
            return
        for holding in holdings.values():
            weights.loc[trade_date, str(holding["ts_code"])] = float(holding["weight"])

    def _build_latest_holdings(self, holdings: dict[str, dict[str, object]]) -> list[dict]:
        rows = sorted(holdings.values(), key=lambda item: (-float(item["score"]), str(item["ts_code"])))
        return [
            {
                "ts_code": str(item["ts_code"]),
                "name": str(item["name"]),
                "industry": str(item["industry"]),
                "score": round(float(item["score"]), 4),
                "selection_reason": str(item["selection_reason"]),
            }
            for item in rows
        ]

    def _get_buy_execution(
        self,
        ts_code: str,
        signal_date: pd.Timestamp,
    ) -> tuple[pd.Timestamp, float] | None:
        return self._get_next_open_execution(ts_code, signal_date, reject_limit_up=True)

    def _get_next_open_sell_execution(
        self,
        ts_code: str,
        signal_date: pd.Timestamp,
    ) -> tuple[pd.Timestamp, float] | None:
        return self._get_next_open_execution(ts_code, signal_date, reject_limit_up=False)

    def _get_next_open_execution(
        self,
        ts_code: str,
        signal_date: pd.Timestamp,
        *,
        reject_limit_up: bool,
    ) -> tuple[pd.Timestamp, float] | None:
        next_trade_date = self._get_next_trade_date(signal_date)
        if next_trade_date is None:
            return None

        stock_data = self._stock_groups.get(ts_code)
        if stock_data is None or next_trade_date not in stock_data.index:
            return None

        row = stock_data.loc[next_trade_date]
        open_price = row.get("open")
        pre_close = row.get("pre_close")
        if pd.isna(open_price) or float(open_price) <= 0:
            return None
        if reject_limit_up and (pd.isna(pre_close) or float(pre_close) <= 0):
            return None
        if reject_limit_up and self._is_open_at_limit_up(ts_code, next_trade_date, row):
            return None
        return next_trade_date, float(open_price)

    def _is_open_at_limit_up(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        row: pd.Series,
    ) -> bool:
        pre_close = row.get("pre_close")
        if pd.isna(pre_close) or float(pre_close) <= 0:
            return False
        limit_ratio = self._get_limit_ratio(ts_code, trade_date, row)
        limit_up_price = self._round_price(float(pre_close) * (1.0 + limit_ratio))
        open_price = float(row["open"])
        return open_price >= limit_up_price - 1e-6

    def _is_close_at_limit_up(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        row: pd.Series,
    ) -> bool:
        pre_close = row.get("pre_close")
        close_price = row.get("close")
        if pd.isna(pre_close) or float(pre_close) <= 0 or pd.isna(close_price) or float(close_price) <= 0:
            return False
        limit_ratio = self._get_limit_ratio(ts_code, trade_date, row)
        limit_up_price = self._round_price(float(pre_close) * (1.0 + limit_ratio))
        return float(close_price) >= limit_up_price - 1e-6

    @staticmethod
    def _get_limit_ratio(ts_code: str, trade_date: pd.Timestamp, row: pd.Series) -> float:
        name = str(row.get("name") or "")
        if "ST" in name:
            return 0.05

        code = str(ts_code).split(".")[0]
        if code.startswith(("8", "4")):
            return 0.30
        if code.startswith(("688", "689")):
            return 0.20
        if code.startswith(("300", "301")) and pd.Timestamp(trade_date) >= pd.Timestamp("2020-08-24"):
            return 0.20
        return 0.10

    @staticmethod
    def _round_price(price: float) -> float:
        return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _get_next_trade_date(self, trade_date: pd.Timestamp) -> pd.Timestamp | None:
        idx = self._date_to_list_idx.get(pd.Timestamp(trade_date))
        if idx is None or idx + 1 >= len(self._trade_dates_list):
            return None
        return self._trade_dates_list[idx + 1]

    def _ensure_chips_for_date(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        warnings: list[str],
    ) -> None:
        target_date = pd.Timestamp(trade_date).normalize()
        date_str = target_date.strftime("%Y%m%d")

        cached = self._chips_cache.get(ts_code)
        if cached is not None and not cached.empty:
            cached_dates = set(pd.to_datetime(cached["trade_date"]).dt.normalize())
            if target_date in cached_dates:
                return

        attempted = self._chips_missing_attempts.setdefault(ts_code, set())
        if date_str in attempted:
            return

        start_idx = self._date_to_list_idx.get(target_date)
        if start_idx is None:
            attempted.add(date_str)
            return

        fetched_end = self._chips_fetched_end.get(ts_code)
        if fetched_end:
            fetched_idx = self._date_to_list_idx.get(pd.Timestamp(fetched_end))
            if fetched_idx is not None and fetched_idx >= start_idx:
                attempted.add(date_str)
                return

        end_idx = min(start_idx + self._CHIPS_CHUNK_TRADING_DAYS - 1, len(self._trade_dates_list) - 1)
        start_str = self._trade_dates_list[start_idx].strftime("%Y%m%d")
        end_str = self._trade_dates_list[end_idx].strftime("%Y%m%d")

        try:
            new_data = self._tushare.get_cyq_chips_range(ts_code, start_str, end_str)
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"{trade_date.strftime('%Y-%m-%d')} {ts_code} 筹码数据拉取失败({start_str}~{end_str}): {exc}"
            )
            attempted.add(date_str)
            return

        if new_data is None or new_data.empty:
            attempted.add(date_str)
            self._chips_fetched_end[ts_code] = end_str
            return

        existing = self._chips_cache.get(ts_code)
        if existing is not None and not existing.empty:
            merged = pd.concat([existing, new_data], ignore_index=True)
            merged = merged.drop_duplicates(subset=["trade_date", "price"], keep="last")
        else:
            merged = new_data.copy()

        self._chips_cache[ts_code] = merged
        self._chips_fetched_end[ts_code] = pd.to_datetime(merged["trade_date"]).max().strftime("%Y%m%d")

        merged_dates = set(pd.to_datetime(merged["trade_date"]).dt.normalize())
        if target_date not in merged_dates:
            attempted.add(date_str)

    def _get_profit_ratio(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        close_price: float,
    ) -> float | None:
        chips = self._chips_cache.get(ts_code)
        if chips is None or chips.empty:
            return None

        td = pd.Timestamp(trade_date).normalize()
        day_chips = chips.loc[pd.to_datetime(chips["trade_date"]).dt.normalize() == td]
        if day_chips.empty:
            return None

        ref_price = close_price
        prices = pd.to_numeric(day_chips["price"], errors="coerce").dropna()
        if not prices.empty:
            above = prices.loc[prices > close_price]
            if not above.empty:
                next_level = float(above.min())
                if close_price > 0 and (next_level - close_price) / close_price < 0.001:
                    ref_price = next_level
        return float(day_chips.loc[pd.to_numeric(day_chips["price"], errors="coerce") <= ref_price, "percent"].sum())

    @staticmethod
    def _get_chip_reference_price(row: pd.Series) -> float:
        if "close_unadj" in row.index and not pd.isna(row["close_unadj"]):
            return float(row["close_unadj"])
        return float(row["close"])
