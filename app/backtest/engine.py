from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import pandas as pd

from app.strategy.base import BaseStrategy


@dataclass
class EngineResult:
    strategy_curve: pd.DataFrame
    benchmark_curves: dict[str, pd.DataFrame]
    metrics: dict[str, dict[str, float]]
    signal_summary: dict[str, float]
    trade_summary: dict[str, float]
    trade_records: list[dict]
    daily_position_details: list[dict]


class BacktestEngine:
    def run(
        self,
        market_data: pd.DataFrame,
        strategy: BaseStrategy,
        trade_dates: list[pd.Timestamp],
        benchmark_data: dict[str, pd.DataFrame],
        initial_capital: float,
        commission_rate: float,
        stamp_duty_rate: float,
    ) -> EngineResult:
        if market_data.empty:
            raise ValueError("股票行情为空，无法执行回测。")
        if not getattr(strategy, "is_portfolio_strategy", False):
            raise ValueError("当前回测入口仅支持组合型选股策略。")

        prepared_market = market_data.copy()
        prepared_market["trade_date"] = pd.to_datetime(prepared_market["trade_date"])
        prepared_market = prepared_market.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

        target_weights, strategy_meta = strategy.generate_portfolio_weights(prepared_market, trade_dates)
        if target_weights.empty:
            raise ValueError("策略未生成任何组合持仓。")

        target_weights.index = pd.to_datetime(target_weights.index)
        target_weights = target_weights.sort_index().fillna(0.0)

        close_matrix = (
            prepared_market.pivot_table(index="trade_date", columns="ts_code", values="close", aggfunc="last")
            .sort_index().ffill()
        )
        close_matrix = close_matrix.reindex(target_weights.index).ffill()
        open_matrix = (
            prepared_market.pivot_table(index="trade_date", columns="ts_code", values="open", aggfunc="last")
            .sort_index().ffill()
        )
        open_matrix = open_matrix.reindex(target_weights.index).ffill()
        target_weights = target_weights.reindex(columns=close_matrix.columns, fill_value=0.0)
        open_matrix = open_matrix.reindex(columns=close_matrix.columns)

        strategy_curve, trade_records, daily_position_details = self._simulate_portfolio(
            target_weights=target_weights,
            close_matrix=close_matrix,
            open_matrix=open_matrix,
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            stamp_duty_rate=stamp_duty_rate,
            buy_prices=strategy_meta.get("buy_prices", {}),
            sell_prices=strategy_meta.get("sell_prices", {}),
            sell_reasons=strategy_meta.get("sell_reasons", {}),
            sell_orders=strategy_meta.get("sell_orders", {}),
            market_data=prepared_market,
        )

        strategy_nav = strategy_curve.set_index("trade_date")["net_value"]
        strategy_returns = strategy_curve.set_index("trade_date")["daily_return"]
        invested_ratio = strategy_curve.set_index("trade_date")["position"]
        holding_count = strategy_curve.set_index("trade_date")["holding_count"]

        trade_summary = self._summarize_trades(trade_records)

        benchmark_curves: dict[str, pd.DataFrame] = {}
        metrics: dict[str, dict[str, float]] = {"策略收益": self._metrics(strategy_nav, strategy_returns, initial_capital)}

        for name, frame in benchmark_data.items():
            curve = frame.copy()
            curve["trade_date"] = pd.to_datetime(curve["trade_date"])
            curve = curve.sort_values("trade_date").set_index("trade_date")
            curve = curve.reindex(strategy_nav.index).ffill().dropna(subset=["close"])
            benchmark_returns = curve["close"].pct_change().fillna(0.0)
            benchmark_nav = (1.0 + benchmark_returns).cumprod()
            benchmark_curves[name] = pd.DataFrame(
                {
                    "trade_date": curve.index,
                    "close": curve["close"].values,
                    "daily_return": benchmark_returns.values,
                    "net_value": benchmark_nav.values,
                    "capital": (initial_capital * benchmark_nav).values,
                }
            )
            metrics[name] = self._metrics(benchmark_nav, benchmark_returns, initial_capital)

        signal_summary = {
            "buy_signals": float((target_weights.diff().fillna(target_weights) > 0).sum().sum()),
            "sell_signals": float((target_weights.diff().fillna(0.0) < 0).sum().sum()),
            "average_position": float(invested_ratio.mean()),
            "average_holding_count": float(holding_count.mean()),
            "rebalance_count": float(strategy_meta.get("rebalance_count", 0)),
            "latest_holdings": strategy_meta.get("latest_holdings", []),
            "warnings": strategy_meta.get("warnings", []),
            "chips_data_source_report": strategy_meta.get("chips_data_source_report", {}),
        }

        return EngineResult(
            strategy_curve=strategy_curve,
            benchmark_curves=benchmark_curves,
            metrics=metrics,
            signal_summary=signal_summary,
            trade_summary=trade_summary,
            trade_records=trade_records,
            daily_position_details=daily_position_details,
        )

    # ─────────────── 真实持股数量模拟 ───────────────

    def _simulate_portfolio(
        self,
        target_weights: pd.DataFrame,
        close_matrix: pd.DataFrame,
        open_matrix: pd.DataFrame,
        initial_capital: float,
        commission_rate: float,
        stamp_duty_rate: float,
        buy_prices: dict[str, float],
        sell_prices: dict[str, float],
        sell_reasons: dict[str, str],
        sell_orders: dict[str, dict[str, object]],
        market_data: pd.DataFrame,
    ) -> tuple[pd.DataFrame, list[dict], list[dict]]:
        name_map = self._build_name_map(market_data)
        asset_returns = close_matrix.pct_change().fillna(0.0)
        dates = list(target_weights.index)

        cash = initial_capital
        positions: dict[str, dict] = {}
        pending_sells: dict[str, dict] = {}

        trade_records: list[dict] = []
        daily_details: list[dict] = []
        nav_list: list[float] = []
        return_list: list[float] = []
        capital_list: list[float] = []
        hc_list: list[int] = []
        ir_list: list[float] = []

        prev_pv = initial_capital
        zero_w = pd.Series(0.0, index=target_weights.columns)

        for idx, trade_date in enumerate(dates):
            signal_date = dates[idx - 1] if idx > 0 else None
            desired = target_weights.loc[signal_date] if signal_date is not None else zero_w
            effective = target_weights.loc[dates[idx - 2]] if idx >= 2 else zero_w

            # ── 1. 执行延迟卖单 (sell_orders) ──
            for ts_code in list(pending_sells.keys()):
                ps = pending_sells[ts_code]
                if pd.Timestamp(ps["exec_date"]) > pd.Timestamp(trade_date):
                    continue
                if ts_code not in positions:
                    del pending_sells[ts_code]
                    continue
                ep = float(ps["exec_price"])
                opened = positions.pop(ts_code)
                sa = opened["shares"] * ep
                cost = sa * (commission_rate + stamp_duty_rate)
                cash += sa - cost
                self._append_sell_record(
                    trade_records, opened, name_map, ep,
                    pd.Timestamp(ps["exec_date"]), ps.get("reason", "调仓卖出"), dates,
                )
                del pending_sells[ts_code]

            # ── 2. 处理常规卖出 & 减仓（先卖回笼现金） ──
            if signal_date is not None:
                for ts_code in target_weights.columns:
                    pw = float(effective.get(ts_code, 0.0))
                    dw = float(desired.get(ts_code, 0.0))

                    if pw > 0 and dw <= 0 and ts_code in positions:
                        sell_key = f"{ts_code}|{signal_date.strftime('%Y-%m-%d')}"

                        if sell_key in sell_orders:
                            order = sell_orders[sell_key] or {}
                            pending_sells[ts_code] = {
                                "exec_date": pd.Timestamp(order.get("execution_date")),
                                "exec_price": float(order.get("execution_price")),
                                "reason": sell_reasons.get(sell_key, "调仓卖出"),
                            }
                            continue

                        ep = self._resolve_sell_price(
                            sell_key, sell_prices, open_matrix, close_matrix,
                            trade_date, ts_code,
                        )
                        if ep is None:
                            continue

                        opened = positions.pop(ts_code)
                        sa = opened["shares"] * ep
                        cost = sa * (commission_rate + stamp_duty_rate)
                        cash += sa - cost
                        sd = signal_date if sell_key in sell_prices else trade_date
                        self._append_sell_record(
                            trade_records, opened, name_map, ep, sd,
                            sell_reasons.get(sell_key, "调仓卖出"), dates,
                        )

                    elif pw > 0 and dw > 0 and dw < pw - 0.005 and ts_code in positions:
                        keep = dw / pw
                        sell_shares = positions[ts_code]["shares"] * (1 - keep)
                        ep = float(open_matrix.loc[trade_date, ts_code])
                        if not pd.isna(ep) and ep > 0:
                            sa = sell_shares * ep
                            cost = sa * (commission_rate + stamp_duty_rate)
                            cash += sa - cost
                        positions[ts_code]["shares"] *= keep
                        positions[ts_code]["buy_amount"] *= keep
                        positions[ts_code]["position_weight"] = dw

            # ── 3. 处理买入 & 补仓 ──
            if signal_date is not None:
                for ts_code in target_weights.columns:
                    pw = float(effective.get(ts_code, 0.0))
                    dw = float(desired.get(ts_code, 0.0))

                    if pw <= 0 and dw > 0:
                        buy_key = f"{ts_code}|{signal_date.strftime('%Y-%m-%d')}"
                        ep = self._resolve_buy_price(buy_key, buy_prices, open_matrix, trade_date, ts_code)
                        if ep is None:
                            continue

                        target_amt = prev_pv * dw
                        max_afford = cash / (1 + commission_rate) if commission_rate > 0 else cash
                        actual = min(target_amt, max_afford)
                        if actual <= 0:
                            continue

                        shares = actual / ep
                        cash -= actual + actual * commission_rate
                        positions[ts_code] = {
                            "shares": shares,
                            "buy_price": ep,
                            "buy_date": trade_date,
                            "buy_amount": actual,
                            "position_weight": dw,
                            "name": name_map.get(ts_code, ts_code),
                        }

                    elif pw > 0 and dw > pw + 0.005 and ts_code in positions:
                        buy_key = f"{ts_code}|{signal_date.strftime('%Y-%m-%d')}"
                        ep = self._resolve_buy_price(buy_key, buy_prices, open_matrix, trade_date, ts_code)
                        if ep is None:
                            continue

                        add_target = prev_pv * (dw - pw)
                        max_afford = cash / (1 + commission_rate) if commission_rate > 0 else cash
                        add_actual = min(add_target, max_afford)
                        if add_actual <= 0:
                            continue

                        add_shares = add_actual / ep
                        cash -= add_actual + add_actual * commission_rate

                        trade_records.append({
                            "trade_type": "补仓",
                            "ts_code": ts_code,
                            "name": positions[ts_code].get("name", name_map.get(ts_code, ts_code)),
                            "buy_date": trade_date.strftime("%Y-%m-%d"),
                            "sell_date": "-",
                            "sell_reason": "调仓补仓",
                            "buy_price": round(ep, 4),
                            "sell_price": 0.0,
                            "position_weight": round(float(dw - pw), 4),
                            "shares": round(float(add_shares), 4),
                            "buy_amount": round(float(add_actual), 2),
                            "sell_amount": 0.0,
                            "pnl_amount": 0.0,
                            "return_rate": 0.0,
                            "holding_days": 0,
                        })

                        old_amt = positions[ts_code]["buy_amount"]
                        old_sh = positions[ts_code]["shares"]
                        new_amt = old_amt + add_actual
                        new_sh = old_sh + add_shares
                        positions[ts_code]["buy_price"] = new_amt / new_sh if new_sh > 0 else ep
                        positions[ts_code]["buy_amount"] = new_amt
                        positions[ts_code]["shares"] = new_sh
                        positions[ts_code]["position_weight"] = dw

            # ── 4. 收盘估值 ──
            hv = 0.0
            day_holdings: list[dict] = []
            for ts_code, pos in positions.items():
                if ts_code not in close_matrix.columns:
                    continue
                cp = float(close_matrix.loc[trade_date, ts_code])
                if pd.isna(cp) or cp <= 0:
                    continue
                mv = pos["shares"] * cp
                hv += mv

                bp = float(pos["buy_price"])
                ba = float(pos["buy_amount"])
                sh = float(pos["shares"])
                dr = float(asset_returns.loc[trade_date, ts_code]) if trade_date in asset_returns.index else 0.0
                tr = float(cp / bp - 1.0) if bp > 0 else 0.0
                prev_cp = None
                if idx > 0:
                    val = float(close_matrix.loc[dates[idx - 1], ts_code])
                    if not pd.isna(val) and val > 0:
                        prev_cp = val
                base = bp if pd.Timestamp(pos["buy_date"]) == trade_date else (prev_cp or bp)
                day_holdings.append({
                    "ts_code": ts_code,
                    "name": pos.get("name", name_map.get(ts_code, ts_code)),
                    "position_weight": 0.0,
                    "close_price": round(cp, 4),
                    "daily_return": round(dr, 6),
                    "total_return": round(tr, 6),
                    "daily_pnl_amount": round(float(mv - sh * base), 2),
                    "floating_pnl_amount": round(float(mv - ba), 2),
                    "buy_date": pd.Timestamp(pos["buy_date"]).strftime("%Y-%m-%d"),
                    "buy_price": round(bp, 4),
                })

            pv = cash + hv
            for h in day_holdings:
                cp = h["close_price"]
                ts = h["ts_code"]
                sh = positions[ts]["shares"] if ts in positions else 0.0
                h["position_weight"] = round(float(sh * cp / pv), 6) if pv > 0 else 0.0

            dr = (pv / prev_pv - 1.0) if prev_pv > 0 else 0.0
            nav = pv / initial_capital
            ir = hv / pv if pv > 0 else 0.0

            nav_list.append(nav)
            return_list.append(dr)
            capital_list.append(pv)
            hc_list.append(len(positions))
            ir_list.append(ir)

            day_holdings.sort(key=lambda x: (-x["position_weight"], -x["total_return"], x["ts_code"]))
            daily_details.append({
                "date": trade_date.strftime("%Y-%m-%d"),
                "capital": round(pv, 2),
                "position": round(ir, 6),
                "holding_count": len(positions),
                "daily_return": round(dr, 6),
                "net_value": round(nav, 6),
                "holdings": day_holdings,
            })
            prev_pv = pv

        if dates:
            final = dates[-1]
            for ts_code, pos in list(positions.items()):
                fp = float(close_matrix.loc[final, ts_code])
                if pd.isna(fp) or fp <= 0:
                    continue
                self._append_sell_record(
                    trade_records, pos, name_map, fp, final, "回测结束持有", dates,
                    trade_type="持有中",
                )

        for i, rec in enumerate(trade_records, start=1):
            rec["trade_no"] = i

        curve = pd.DataFrame({
            "trade_date": dates,
            "daily_return": return_list,
            "net_value": nav_list,
            "capital": capital_list,
            "holding_count": hc_list,
            "position": ir_list,
        })
        return curve, trade_records, daily_details

    # ─────────────── 辅助方法 ───────────────

    def _resolve_buy_price(
        self, buy_key: str, buy_prices: dict, open_matrix: pd.DataFrame,
        trade_date: pd.Timestamp, ts_code: str,
    ) -> float | None:
        if buy_key in buy_prices:
            p = float(buy_prices[buy_key])
        else:
            p = float(open_matrix.loc[trade_date, ts_code])
        return None if pd.isna(p) or p <= 0 else p

    def _resolve_sell_price(
        self, sell_key: str, sell_prices: dict,
        open_matrix: pd.DataFrame, close_matrix: pd.DataFrame,
        trade_date: pd.Timestamp, ts_code: str,
    ) -> float | None:
        if sell_key in sell_prices:
            p = float(sell_prices[sell_key])
        else:
            p = float(open_matrix.loc[trade_date, ts_code])
        return None if pd.isna(p) or p <= 0 else p

    def _append_sell_record(
        self, records: list[dict], opened: dict, name_map: dict,
        sell_price: float, sell_date: pd.Timestamp, sell_reason: str,
        dates: list[pd.Timestamp], *, trade_type: str = "卖出",
    ) -> None:
        sa = opened["shares"] * sell_price
        pnl = sa - opened["buy_amount"]
        ret = pnl / opened["buy_amount"] if opened["buy_amount"] > 0 else 0.0
        bd = pd.Timestamp(opened["buy_date"])
        sd = pd.Timestamp(sell_date)
        hd = int(len([d for d in dates if bd <= d <= sd]) - 1)
        records.append({
            "trade_type": trade_type,
            "ts_code": opened.get("ts_code", ""),
            "name": opened.get("name", name_map.get(opened.get("ts_code", ""), "")),
            "buy_date": bd.strftime("%Y-%m-%d"),
            "sell_date": sd.strftime("%Y-%m-%d"),
            "sell_reason": sell_reason,
            "buy_price": round(float(opened["buy_price"]), 4),
            "sell_price": round(float(sell_price), 4),
            "position_weight": round(float(opened.get("position_weight", 0)), 4),
            "shares": round(float(opened["shares"]), 4),
            "buy_amount": round(float(opened["buy_amount"]), 2),
            "sell_amount": round(float(sa), 2),
            "pnl_amount": round(float(pnl), 2),
            "return_rate": round(float(ret), 6),
            "holding_days": hd,
        })

    def _metrics(self, nav: pd.Series, daily_returns: pd.Series, initial_capital: float) -> dict[str, float]:
        total_return = float(nav.iloc[-1] - 1.0)
        periods = max(len(nav), 1)
        annualized_return = float((nav.iloc[-1] ** (252 / periods)) - 1.0) if periods > 1 else 0.0
        drawdown = nav / nav.cummax() - 1.0
        max_drawdown = float(drawdown.min())
        volatility = float(daily_returns.std(ddof=0) * sqrt(252))
        std = daily_returns.std(ddof=0)
        sharpe_ratio = float(daily_returns.mean() / std * sqrt(252)) if std > 0 else 0.0
        win_rate = float((daily_returns > 0).sum() / len(daily_returns)) if len(daily_returns) else 0.0
        final_value = float(initial_capital * nav.iloc[-1])
        return {
            "total_return": total_return,
            "annualized_return": annualized_return,
            "max_drawdown": max_drawdown,
            "volatility": volatility,
            "sharpe_ratio": sharpe_ratio,
            "win_rate": win_rate,
            "final_value": final_value,
        }

    def _build_name_map(self, market_data: pd.DataFrame) -> dict[str, str]:
        if market_data.empty:
            return {}
        frame = (
            market_data.dropna(subset=["ts_code"])
            .drop_duplicates(subset=["ts_code"], keep="last")
            .set_index("ts_code")
        )
        if "name" not in frame.columns:
            return {str(ts_code): str(ts_code) for ts_code in frame.index}
        return {str(ts_code): str(frame.loc[ts_code, "name"]) for ts_code in frame.index}

    def _summarize_trades(self, trade_records: list[dict]) -> dict[str, float]:
        empty = {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "win_rate": 0.0, "total_pnl_amount": 0.0, "average_pnl_amount": 0.0,
            "average_return_rate": 0.0, "best_trade_return": 0.0, "worst_trade_return": 0.0,
        }
        if not trade_records:
            return empty
        frame = pd.DataFrame(trade_records)
        if "trade_type" in frame.columns:
            frame = frame.loc[~frame["trade_type"].isin({"补仓", "持有中"})].copy()
        if frame.empty:
            return empty
        winning = int((frame["pnl_amount"] > 0).sum())
        losing = int((frame["pnl_amount"] < 0).sum())
        return {
            "total_trades": int(len(frame)),
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": float(winning / len(frame)) if len(frame) else 0.0,
            "total_pnl_amount": float(frame["pnl_amount"].sum()),
            "average_pnl_amount": float(frame["pnl_amount"].mean()),
            "average_return_rate": float(frame["return_rate"].mean()),
            "best_trade_return": float(frame["return_rate"].max()),
            "worst_trade_return": float(frame["return_rate"].min()),
        }
