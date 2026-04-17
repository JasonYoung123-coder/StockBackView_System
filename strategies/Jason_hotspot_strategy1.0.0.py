from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.tushare_client import TushareClient
from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    name = "劲帆短线热点追涨1.0.0"
    description = (
        "3日涨幅>10%热股 + 主力资金排名 + 板块概念标签打分，"
        "T+1开盘买入，持仓4天亏损卖出，筹码止盈+低点止损，最多持仓3只。"
    )
    is_portfolio_strategy = True
    lookback_days = 60

    max_holdings = 3
    position_per_stock = 1.0 / 3

    # ── 选股参数 ──
    cumulative_gain_days = 3
    cumulative_gain_threshold = 0.10
    top_board_count = 20
    chase_max_pct_chg = 5.0

    # ── 持仓参数 ──
    rebalance_holding_days = 4
    rebalance_loss_threshold = 0.0

    # ── 止盈参数 ──
    take_profit_chips_threshold = 99.0
    take_profit_prev_day_chips_threshold = 99.0
    take_profit_volume_lookback = 30
    take_profit_near_threshold_gap = 2.0
    chips_prefetch_days = 60

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

        self.report_progress(55, "正在预计算技术指标")
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
        self._board_cache: dict[str, set[str]] = {}
        self._warnings: list[str] = []
        self._warning_keys: set[str] = set()

        trade_index = pd.DatetimeIndex(trade_dates)
        self._backtest_start_date = pd.Timestamp(trade_index.min()).normalize()
        self._backtest_end_date = pd.Timestamp(trade_index.max()).normalize()
        self._trade_dates_list: list[pd.Timestamp] = list(trade_index)
        self._date_to_list_idx: dict[pd.Timestamp, int] = {
            d: i for i, d in enumerate(self._trade_dates_list)
        }
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

            # ── 买入判断 ──
            if len(holdings) < self.max_holdings:
                added = self._fill_positions(prepared, trade_date, holdings, buy_prices)
                if added > 0:
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

        for col in ("open", "high", "low", "close", "vol", "amount", "pct_chg"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

        block_words = ["ST", "*ST", "S*ST", "SST", "退", "N ", "C "]
        for word in block_words:
            frame = frame[~frame["name"].str.contains(word, na=False, regex=False)]

        frame = frame.dropna(subset=["open", "close", "high", "low", "vol"])
        frame = frame.reset_index(drop=True)
        return frame

    # ─────────────────── 预计算指标 ───────────────────

    def _precompute_indicators(self, frame: pd.DataFrame) -> None:
        ts_col = frame["ts_code"]

        def _gt(col: str, func):
            return frame.groupby(ts_col, sort=False)[col].transform(func)

        n = self.cumulative_gain_days
        frame["cum_gain_3d"] = _gt(
            "close", lambda s: s / s.shift(n) - 1,
        )

        frame["is_limit_up"] = frame["pct_chg"] >= 9.8

        _prev_vol = _gt("vol", lambda s: s.shift(1))
        frame["is_shrink_limit_up"] = (
            frame["is_limit_up"]
            & (_prev_vol > 0)
            & (frame["vol"] < _prev_vol)
        )

        frame["has_shrink_limit_up_3d"] = _gt(
            "is_shrink_limit_up",
            lambda s: s.astype(float).rolling(n, min_periods=1).max(),
        ).astype(bool)

        frame["vol_max_30d"] = _gt(
            "vol", lambda s: s.rolling(30, min_periods=1).max(),
        )

        _prev_low = _gt("low", lambda s: s.shift(1))
        frame["stop_loss_ref"] = frame[["low"]].assign(prev_low=_prev_low).min(axis=1)

    # ─────────────────────── 卖出逻辑 ───────────────────────

    def _should_sell(
        self,
        ts_code: str,
        trade_date: pd.Timestamp,
        holding: dict,
    ) -> tuple[bool, str, float | None]:
        stock_data = self._stock_groups.get(ts_code)
        if stock_data is None or stock_data.empty:
            return True, "数据缺失", None

        recent = stock_data.loc[stock_data["trade_date"] <= trade_date]
        if len(recent) < 2:
            return False, "", None

        latest = recent.iloc[-1]
        latest_close = float(latest["close"])

        # ── 止损: 收盘跌破 min(买入日low, 买入前日low) ──
        stop_loss_price = holding.get("stop_loss_price", 0.0)
        if stop_loss_price > 0 and latest_close < stop_loss_price:
            return True, "止损-跌破买入日低点", None

        # ── 止盈: 筹码获利盘>99% + 30日最大量 + 非涨停 ──
        lookback = self.take_profit_volume_lookback
        vol_tail = recent.tail(lookback)
        is_vol_max = (
            len(vol_tail) >= lookback
            and float(latest["vol"]) >= float(vol_tail["vol"].max())
        )
        is_not_limit_up = float(latest.get("pct_chg", 0)) < 9.8

        if is_vol_max and is_not_limit_up:
            self._ensure_chips(ts_code, holding["buy_date"], trade_date)
            close_price = float(latest["close"])

            today_ratio = self._get_profit_ratio(ts_code, trade_date, close_price)
            if today_ratio is not None and today_ratio > self.take_profit_chips_threshold:
                return True, "止盈-筹码获利盘过高+放量", close_price - 0.01
            self._warn_near_take_profit(
                ts_code, trade_date, today_ratio, "当日",
                self.take_profit_chips_threshold,
            )

            if len(recent) >= 2:
                prev = recent.iloc[-2]
                prev_close = float(prev["close"])
                prev_ratio = self._get_profit_ratio(
                    ts_code, prev["trade_date"], prev_close,
                )
                if prev_ratio is not None and prev_ratio > self.take_profit_prev_day_chips_threshold:
                    return True, "止盈-前日筹码获利盘过高+放量", close_price - 0.01
                self._warn_near_take_profit(
                    ts_code, trade_date, prev_ratio, "前一日",
                    self.take_profit_prev_day_chips_threshold,
                )

        # ── 调仓: 持仓满N个交易日后亏损卖出 ──
        buy_date = pd.Timestamp(holding["buy_date"])
        buy_idx = self._date_to_list_idx.get(buy_date)
        trade_idx = self._date_to_list_idx.get(trade_date)
        if buy_idx is not None and trade_idx is not None:
            holding_trade_days = trade_idx - buy_idx
            if holding_trade_days >= self.rebalance_holding_days:
                current_return = (latest_close / holding["buy_price"]) - 1
                if current_return < self.rebalance_loss_threshold:
                    return (
                        True,
                        f"调仓-持仓{holding_trade_days}天收益{current_return:.1%}",
                        None,
                    )

        return False, "", None

    # ─────────────────────── 买入逻辑 ───────────────────────

    def _fill_positions(
        self,
        prepared: pd.DataFrame,
        trade_date: pd.Timestamp,
        holdings: dict[str, dict],
        buy_prices: dict[str, float],
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
            execution_date, execution_price = self._get_buy_execution(row.ts_code, trade_date)
            if execution_date is None or execution_price is None:
                continue

            signal_row = prepared.loc[
                (prepared["ts_code"] == row.ts_code) & (prepared["trade_date"] == trade_date)
            ]
            stop_loss_price = (
                float(signal_row.iloc[0]["stop_loss_ref"])
                if not signal_row.empty and pd.notna(signal_row.iloc[0].get("stop_loss_ref"))
                else 0.0
            )

            holdings[row.ts_code] = {
                "ts_code": row.ts_code,
                "name": getattr(row, "name", ""),
                "industry": getattr(row, "industry", "未知"),
                "score": float(row.total_score),
                "selection_reason": (
                    f"热点追涨 资金排名={int(row.mf_rank)}"
                    f" 板块加分={row.board_bonus:.1f}"
                ),
                "buy_date": execution_date,
                "buy_price": execution_price,
                "stop_loss_price": stop_loss_price,
                "weight": self.position_per_stock,
            }
            self._prefetch_full_backtest_chips(row.ts_code)
            buy_prices[f"{row.ts_code}|{trade_date.strftime('%Y-%m-%d')}"] = execution_price
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
        date_str = as_of_date.strftime("%Y%m%d")

        # ── 第一步：基础筛选 ──
        mask = (
            (~day["ts_code"].isin(held))
            & (day["cum_gain_3d"].notna())
            & (day["cum_gain_3d"] >= self.cumulative_gain_threshold)
            & (~day["has_shrink_limit_up_3d"])
            & (~day["is_limit_up"])
            & (day["pct_chg"] < self.chase_max_pct_chg)
        )
        c = day.loc[mask].copy()
        if c.empty:
            return pd.DataFrame()

        # ── 第二步：主力资金排名打分 ──
        mf_ths = self._tushare.get_moneyflow_ths_by_trade_date(date_str)
        if mf_ths.empty:
            return pd.DataFrame()

        mf_ths["net_amount"] = pd.to_numeric(mf_ths["net_amount"], errors="coerce").fillna(0)
        c = c.merge(
            mf_ths[["ts_code", "net_amount"]],
            on="ts_code",
            how="left",
            suffixes=("", "_mf"),
        )
        c["net_amount"] = c["net_amount"].fillna(0)
        c["mf_rank"] = c["net_amount"].rank(ascending=False, method="min")
        c["score"] = np.where(
            c["mf_rank"] <= 1, 3,
            np.where(c["mf_rank"] <= 5, 2,
                     np.where(c["mf_rank"] <= 10, 1, 0)),
        )
        c = c.loc[c["score"] > 0].copy()
        if c.empty:
            return pd.DataFrame()

        # ── 第三步：板块概念标签加分 ──
        mf_cnt = self._tushare.get_moneyflow_cnt_ths_by_trade_date(date_str)
        top_boards: set[str] = set()
        if not mf_cnt.empty:
            mf_cnt["net_amount"] = pd.to_numeric(mf_cnt["net_amount"], errors="coerce").fillna(0)
            top_boards = set(
                mf_cnt.nlargest(self.top_board_count, "net_amount")["ts_code"].tolist()
            )

        board_bonus_list = []
        for ts_code in c["ts_code"].values:
            if top_boards:
                stock_boards = self._get_stock_boards(ts_code)
                overlap = len(stock_boards & top_boards)
            else:
                overlap = 0
            board_bonus_list.append(overlap * 0.5)
        c["board_bonus"] = board_bonus_list
        c["total_score"] = c["score"] + c["board_bonus"]

        # ── 第四步：排序 ──
        c = c.sort_values(
            by=["total_score", "net_amount"],
            ascending=[False, False],
        ).reset_index(drop=True)

        return c.head(self.max_holdings)

    def _get_stock_boards(self, ts_code: str) -> set[str]:
        if ts_code in self._board_cache:
            return self._board_cache[ts_code]
        try:
            member_df = self._tushare.get_ths_member_by_stock(ts_code)
            boards = set(member_df["ts_code"].tolist()) if not member_df.empty else set()
        except Exception:
            boards = set()
        self._board_cache[ts_code] = boards
        return boards

    # ─────────────────────── 买入执行 ───────────────────────

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
        except Exception as exc:
            self._add_warning(
                ts_code, self._backtest_end_date,
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
            self._chips_fetched_end[ts_code] = (
                pd.to_datetime(merged["trade_date"]).max().strftime("%Y%m%d")
            )
        self._chips_full_range_requested.add(ts_code)

    def _ensure_chips(
        self,
        ts_code: str,
        buy_date: pd.Timestamp,
        trade_date: pd.Timestamp,
    ) -> None:
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
            target_indexes = [i for i in target_indexes if i is not None]
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
        except Exception as exc:
            self._add_warning(ts_code, trade_date, f"筹码缺口补拉失败: {exc}")
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
            self._chips_fetched_end[ts_code] = (
                pd.to_datetime(merged["trade_date"]).max().strftime("%Y%m%d")
            )
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
        chips = self._chips_cache.get(ts_code)
        if chips is None or chips.empty:
            return None
        td = pd.Timestamp(trade_date)
        day_chips = chips.loc[chips["trade_date"] == td]
        if day_chips.empty:
            return None
        distribution = day_chips.loc[
            pd.to_numeric(day_chips.get("price"), errors="coerce").notna()
            & pd.to_numeric(day_chips.get("percent"), errors="coerce").notna()
        ].copy()
        if not distribution.empty:
            return float(
                distribution.loc[distribution["price"] <= close_price, "percent"].sum()
            )

        summary_ratio = pd.to_numeric(day_chips.get("profit_ratio"), errors="coerce").dropna()
        if not summary_ratio.empty:
            return float(summary_ratio.iloc[-1])
        return None

    # ─────────────────────── 辅助方法 ───────────────────────

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
                f"接近止盈但未触发：{label}获利筹码占比 {float(ratio):.2f}%，"
                f"未超过阈值 {threshold:.2f}%",
            )

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
