"""实盘历史回放模拟器。

用历史日线数据逐交易日模拟调度器的信号生成 + T+1 执行流程，
输出每日信号和持仓日志，用于在本地快速发现实盘逻辑 bug。

Usage:
    python run_replay.py --strategy Jason_selector_strategy2.0.3 \
        --start 2025-01-01 --end 2025-06-30 --capital 100000
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.services.tushare_client import TushareClient
from app.services.market_data_service import MarketDataService
from app.strategy.loader import StrategyLoader
from app.trading.models import AccountInfo, PositionItem
from app.trading.order_generator import _round_to_lot


# ═══════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════

@dataclass
class ReplayConfig:
    strategy_name: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000.0
    fund_ratio: float = 1.0
    buy_existing: bool = False
    lookback_days: int = 250
    live_start_date: str = ""
    commission_rate: float = 0.0003
    stamp_duty_rate: float = 0.001
    verbose: bool = False


@dataclass
class MockPosition:
    ts_code: str
    name: str
    volume: int
    cost_price: float
    latest_price: float
    buy_date: str


@dataclass
class ReplayDay:
    date: str
    executed_sells: list[dict] = field(default_factory=list)
    executed_buys: list[dict] = field(default_factory=list)
    sell_signals: list[dict] = field(default_factory=list)
    buy_signals: list[dict] = field(default_factory=list)
    pending_sell_signals: list[dict] = field(default_factory=list)
    pending_buy_signals: list[dict] = field(default_factory=list)
    portfolio: list[dict] = field(default_factory=list)
    cash: float = 0.0
    total_asset: float = 0.0
    holding_count: int = 0
    warnings: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# MockBroker
# ═══════════════════════════════════════════════════════════

class MockBroker:
    """模拟券商：追踪账户资金和持仓，模拟订单成交。"""

    def __init__(
        self,
        initial_capital: float,
        fund_ratio: float = 1.0,
        commission_rate: float = 0.0003,
        stamp_duty_rate: float = 0.001,
    ) -> None:
        self.initial_capital = initial_capital
        self.fund_ratio = fund_ratio
        self.commission_rate = commission_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.cash = initial_capital
        self.positions: dict[str, MockPosition] = {}
        self.trade_log: list[dict] = []

    @property
    def market_value(self) -> float:
        return sum(p.volume * p.latest_price for p in self.positions.values())

    @property
    def total_asset(self) -> float:
        return self.cash + self.market_value

    def query_account(self) -> AccountInfo:
        return AccountInfo(
            total_asset=round(self.total_asset, 2),
            available_cash=round(self.cash, 2),
            market_value=round(self.market_value, 2),
            frozen_cash=0.0,
        )

    def query_positions(self) -> list[PositionItem]:
        items = []
        for p in self.positions.values():
            mv = p.volume * p.latest_price
            profit = mv - p.volume * p.cost_price
            profit_rate = profit / (p.volume * p.cost_price) if p.cost_price > 0 else 0.0
            items.append(PositionItem(
                ts_code=p.ts_code,
                name=p.name,
                volume=p.volume,
                available_volume=p.volume,
                cost_price=round(p.cost_price, 3),
                market_value=round(mv, 2),
                profit=round(profit, 2),
                profit_rate=round(profit_rate, 4),
            ))
        return items

    def held_codes(self) -> set[str]:
        return {code for code, p in self.positions.items() if p.volume > 0}

    def execute_sell(self, ts_code: str, price: float, date: str) -> dict | None:
        pos = self.positions.get(ts_code)
        if pos is None or pos.volume <= 0:
            return None

        volume = pos.volume
        amount = volume * price
        commission = amount * self.commission_rate
        stamp_duty = amount * self.stamp_duty_rate
        net_proceeds = amount - commission - stamp_duty
        self.cash += net_proceeds

        pnl = net_proceeds - volume * pos.cost_price
        record = {
            "date": date,
            "direction": "sell",
            "ts_code": ts_code,
            "name": pos.name,
            "volume": volume,
            "price": round(price, 3),
            "amount": round(amount, 2),
            "commission": round(commission, 2),
            "stamp_duty": round(stamp_duty, 2),
            "pnl": round(pnl, 2),
            "cost_price": round(pos.cost_price, 3),
            "buy_date": pos.buy_date,
        }
        self.trade_log.append(record)
        del self.positions[ts_code]
        return record

    def execute_buy(self, ts_code: str, price: float, name: str, date: str) -> dict | None:
        total_for_strategy = self.total_asset * self.fund_ratio
        per_stock_value = total_for_strategy * 0.2
        buy_value = min(per_stock_value, self.cash)
        if buy_value <= 0:
            return None

        buy_vol = _round_to_lot(buy_value / price, ts_code)
        if buy_vol <= 0:
            return None

        amount = buy_vol * price
        commission = amount * self.commission_rate
        total_cost = amount + commission

        if total_cost > self.cash:
            buy_vol = _round_to_lot((self.cash - commission) / price, ts_code)
            if buy_vol <= 0:
                return None
            amount = buy_vol * price
            commission = amount * self.commission_rate
            total_cost = amount + commission

        self.cash -= total_cost

        if ts_code in self.positions:
            old = self.positions[ts_code]
            new_volume = old.volume + buy_vol
            new_cost = (old.volume * old.cost_price + amount) / new_volume
            old.volume = new_volume
            old.cost_price = new_cost
            old.latest_price = price
        else:
            self.positions[ts_code] = MockPosition(
                ts_code=ts_code, name=name, volume=buy_vol,
                cost_price=price, latest_price=price, buy_date=date,
            )

        record = {
            "date": date,
            "direction": "buy",
            "ts_code": ts_code,
            "name": name,
            "volume": buy_vol,
            "price": round(price, 3),
            "amount": round(amount, 2),
            "commission": round(commission, 2),
        }
        self.trade_log.append(record)
        return record

    def update_prices(self, close_prices: dict[str, float]) -> None:
        for code, pos in self.positions.items():
            if code in close_prices:
                pos.latest_price = close_prices[code]


# ═══════════════════════════════════════════════════════════
# ReplayEngine
# ═══════════════════════════════════════════════════════════

class ReplayEngine:
    """历史回放引擎：逐交易日模拟调度器信号生成 + T+1 执行。"""

    def __init__(self, config: ReplayConfig) -> None:
        self.config = config
        self.broker = MockBroker(
            initial_capital=config.initial_capital,
            fund_ratio=config.fund_ratio,
            commission_rate=config.commission_rate,
            stamp_duty_rate=config.stamp_duty_rate,
        )
        self.days: list[ReplayDay] = []
        self.pending_sell_signals: list[dict] = []
        self.pending_buy_signals: list[dict] = []
        self.strategy_target_holdings: int = 0
        self.strategy_holding_codes: set[str] = set()

        self._market_data: pd.DataFrame = pd.DataFrame()
        self._all_trade_dates: list[pd.Timestamp] = []
        self._replay_trade_dates: list[pd.Timestamp] = []
        self._strategy = None
        self._name_map: dict[str, str] = {}

    def run(self) -> list[ReplayDay]:
        self._load_data()
        self._load_strategy()

        total = len(self._replay_trade_dates)
        if total == 0:
            print("回放区间内无交易日。")
            return []

        print(f"\n{'═' * 60}")
        print(f"  实盘回放模拟")
        print(f"  策略: {self.config.strategy_name}")
        print(f"  区间: {self.config.start_date} → {self.config.end_date} ({total} 个交易日)")
        print(f"  初始资金: {self.config.initial_capital:,.2f}")
        print(f"  资金比例: {self.config.fund_ratio:.0%}")
        print(f"  live_start: {self.config.live_start_date or self.config.start_date}")
        print(f"{'═' * 60}\n")

        for idx, day_ts in enumerate(self._replay_trade_dates):
            day_str = day_ts.strftime("%Y-%m-%d")
            day_record = self._simulate_day(day_ts, day_str)
            self.days.append(day_record)

            sell_n = len(day_record.executed_sells)
            buy_n = len(day_record.executed_buys)
            sig_sell = len(day_record.pending_sell_signals)
            sig_buy = len(day_record.pending_buy_signals)
            print(
                f"[{idx + 1:>3}/{total}] {day_str}  "
                f"成交: 卖{sell_n} 买{buy_n}  "
                f"信号: 卖{sig_sell} 买{sig_buy}  "
                f"持仓:{day_record.holding_count}  "
                f"总资产:{day_record.total_asset:>12,.2f}"
            )

            if self.config.verbose:
                for s in day_record.executed_sells:
                    print(f"        卖出 {s['ts_code']} {s.get('name','')} "
                          f"{s['volume']}股 @{s['price']:.2f} 盈亏:{s.get('pnl',0):+,.2f}")
                for b in day_record.executed_buys:
                    print(f"        买入 {b['ts_code']} {b.get('name','')} "
                          f"{b['volume']}股 @{b['price']:.2f}")
                for sig in day_record.pending_sell_signals:
                    print(f"        → 待卖出 {sig['ts_code']} {sig.get('name','')} ({sig.get('reason','')})")
                for sig in day_record.pending_buy_signals:
                    print(f"        → 待买入 {sig['ts_code']} {sig.get('name','')}")
                if day_record.warnings:
                    for w in day_record.warnings[-5:]:
                        print(f"        [警告] {w}")

        self._print_summary()
        self._save_results()
        return self.days

    # ── 数据加载 ──

    def _load_data(self) -> None:
        print("加载市场数据...")
        t0 = time.time()

        tushare_client = TushareClient()
        market_service = MarketDataService(tushare_client=tushare_client)

        start_dt = pd.to_datetime(self.config.start_date)
        end_dt = pd.to_datetime(self.config.end_date)
        lookback_start = start_dt - datetime.timedelta(days=int(self.config.lookback_days * 1.6))

        self._market_data, self._all_trade_dates = market_service.get_market_history(
            lookback_start.strftime("%Y%m%d"),
            end_dt.strftime("%Y%m%d"),
            lookback_days=0,
            progress_callback=lambda pct, msg: print(f"  {msg} ({pct:.0f}%)", end="\r"),
        )
        print()

        self._replay_trade_dates = [
            d for d in self._all_trade_dates
            if start_dt <= d <= end_dt
        ]

        if not self._market_data.empty and "name" in self._market_data.columns:
            dedup = self._market_data.drop_duplicates(subset=["ts_code"], keep="last")
            self._name_map = dict(zip(
                dedup["ts_code"].astype(str),
                dedup["name"].astype(str),
            ))

        elapsed = time.time() - t0
        print(f"数据加载完成: {len(self._all_trade_dates)} 个交易日, "
              f"回放区间 {len(self._replay_trade_dates)} 天 ({elapsed:.1f}s)")

    def _load_strategy(self) -> None:
        print(f"加载策略: {self.config.strategy_name}")
        loader = StrategyLoader()
        loaded = loader.get_strategy(self.config.strategy_name)
        self._strategy = loaded

    # ── 单日模拟 ──

    def _simulate_day(self, day_ts: pd.Timestamp, day_str: str) -> ReplayDay:
        record = ReplayDay(date=day_str)

        # Step 1 & 2: 执行前一天的待执行信号（以今日开盘价成交）
        open_prices = self._get_prices(day_ts, col="open")
        record.executed_sells = self._execute_pending_sells(day_str, open_prices)
        record.executed_buys = self._execute_pending_buys(day_str, open_prices)

        # Step 3: 更新持仓价格为今日收盘价
        close_prices = self._get_prices(day_ts, col="close")
        self.broker.update_prices(close_prices)

        # Step 4: 运行策略
        signals = self._run_strategy(day_ts)
        if signals is None:
            record.warnings.append("策略运行失败")
            self._fill_record_state(record)
            return record

        record.sell_signals = signals.get("sell_signals", [])
        record.buy_signals = signals.get("buy_signals", [])
        record.warnings = signals.get("warnings", [])
        record.diagnostics = signals.get("diagnostics", [])

        # Step 5: 处理卖出信号
        self.pending_sell_signals = self._process_sell_signals(signals)
        record.pending_sell_signals = list(self.pending_sell_signals)

        # Step 6: 处理买入信号
        self.pending_buy_signals, target, codes = self._process_buy_signals(signals)
        self.strategy_target_holdings = target
        self.strategy_holding_codes = codes
        record.pending_buy_signals = list(self.pending_buy_signals)

        self._fill_record_state(record)
        return record

    def _fill_record_state(self, record: ReplayDay) -> None:
        record.cash = round(self.broker.cash, 2)
        record.total_asset = round(self.broker.total_asset, 2)
        record.holding_count = len(self.broker.held_codes())
        record.portfolio = [
            {
                "ts_code": p.ts_code,
                "name": p.name,
                "volume": p.volume,
                "cost_price": round(p.cost_price, 3),
                "latest_price": round(p.latest_price, 3),
                "market_value": round(p.volume * p.latest_price, 2),
                "buy_date": p.buy_date,
            }
            for p in self.broker.positions.values()
        ]

    # ── 价格提取 ──

    def _get_prices(self, day_ts: pd.Timestamp, col: str = "close") -> dict[str, float]:
        day_data = self._market_data[
            pd.to_datetime(self._market_data["trade_date"]).dt.normalize() == day_ts
        ]
        prices: dict[str, float] = {}
        for _, row in day_data.iterrows():
            ts_code = str(row["ts_code"])
            val = float(row.get(col, 0))
            if val > 0:
                prices[ts_code] = val
        return prices

    # ── 执行待处理信号 ──

    def _execute_pending_sells(self, date: str, prices: dict[str, float]) -> list[dict]:
        executed = []
        for sig in self.pending_sell_signals:
            ts_code = sig["ts_code"]
            price = prices.get(ts_code)
            if price is None or price <= 0:
                continue
            result = self.broker.execute_sell(ts_code, price, date)
            if result:
                result["reason"] = sig.get("reason", "")
                executed.append(result)
        self.pending_sell_signals = []
        return executed

    def _execute_pending_buys(self, date: str, prices: dict[str, float]) -> list[dict]:
        executed = []

        # 兜底仓位校验（复刻 scheduler:1262-1284）
        if self.strategy_target_holdings > 0:
            held = self.broker.held_codes()
            pending_buy_codes = {sig["ts_code"] for sig in self.pending_buy_signals}
            strategy_current = len(held & (self.strategy_holding_codes - pending_buy_codes))
            max_buys = max(0, self.strategy_target_holdings - strategy_current)
            if max_buys < len(self.pending_buy_signals):
                self.pending_buy_signals = self.pending_buy_signals[:max_buys]

        for sig in self.pending_buy_signals:
            ts_code = sig["ts_code"]
            price = prices.get(ts_code)
            if price is None or price <= 0:
                continue
            name = sig.get("name", "") or self._name_map.get(ts_code, "")
            result = self.broker.execute_buy(ts_code, price, name, date)
            if result:
                executed.append(result)
        self.pending_buy_signals = []
        return executed

    # ── 策略调用 ──

    def _run_strategy(self, day_ts: pd.Timestamp) -> dict | None:
        data_slice = self._market_data[
            pd.to_datetime(self._market_data["trade_date"]).dt.normalize() <= day_ts
        ].copy()
        dates_slice = [d for d in self._all_trade_dates if d <= day_ts]

        if data_slice.empty or not dates_slice:
            return None

        today_rows = data_slice[
            pd.to_datetime(data_slice["trade_date"]).dt.normalize() == day_ts
        ]
        realtime_prices: dict[str, dict] = {}
        for _, row in today_rows.iterrows():
            ts_code = str(row["ts_code"])
            close = float(row.get("close", 0))
            if close > 0:
                realtime_prices[ts_code] = {"latest_price": close}

        live_start = self.config.live_start_date or self.config.start_date
        held = self.broker.held_codes()

        try:
            if hasattr(self._strategy.instance, "generate_live_signals"):
                signals = self._strategy.instance.generate_live_signals(
                    data_slice, dates_slice, realtime_prices,
                    live_start_date=live_start,
                    qmt_held_codes=held,
                )
            else:
                weights, meta = self._strategy.instance.generate_portfolio_weights(
                    data_slice, dates_slice,
                )
                signals = {
                    "sell_signals": [],
                    "buy_signals": [],
                    "holdings": meta.get("latest_holdings", []),
                }
            return signals
        except Exception as exc:
            if self.config.verbose:
                print(f"        [错误] 策略执行异常: {exc}")
            return None

    # ── 信号处理（复刻 scheduler 逻辑）──

    def _process_sell_signals(self, signals: dict) -> list[dict]:
        """复刻 scheduler._generate_sell_signals (867-933)"""
        sell_signals = signals.get("sell_signals", [])
        held = self.broker.held_codes()

        pending = []
        for sig in sell_signals:
            ts_code = sig["ts_code"]
            if ts_code not in held:
                continue
            pending.append({
                "ts_code": ts_code,
                "name": sig.get("name", "") or self._name_map.get(ts_code, ""),
                "reason": sig.get("reason", "策略卖出"),
            })
        return pending

    def _process_buy_signals(self, signals: dict) -> tuple[list[dict], int, set[str]]:
        """复刻 scheduler._generate_buy_signals (937-1025)"""
        buy_signals = signals.get("buy_signals", [])
        strategy_holdings = signals.get("holdings", [])
        held = self.broker.held_codes()

        if isinstance(strategy_holdings, dict):
            strategy_codes = set(strategy_holdings.keys())
        else:
            strategy_codes = {h.get("ts_code", "") for h in strategy_holdings if h.get("ts_code")}
        strategy_target = len(strategy_codes) or len(strategy_holdings)

        pending = []
        for sig in buy_signals:
            ts_code = sig["ts_code"]
            if not self.config.buy_existing and ts_code in held:
                continue
            pending.append({
                "ts_code": ts_code,
                "name": sig.get("name", "") or self._name_map.get(ts_code, ""),
            })

        strategy_held_count = len(held & strategy_codes)
        pending_sell_count = len(self.pending_sell_signals)
        available_slots = max(0, strategy_target - strategy_held_count + pending_sell_count)
        if len(pending) > available_slots:
            if self.config.verbose:
                for p in pending[available_slots:]:
                    print(f"        [仓位] {p['ts_code']} 超出可用仓位 "
                          f"({available_slots}/{strategy_target})，跳过")
            pending = pending[:available_slots]

        return pending, strategy_target, strategy_codes

    # ── 输出 ──

    def _print_summary(self) -> None:
        if not self.days:
            return

        total_sells = sum(len(d.executed_sells) for d in self.days)
        total_buys = sum(len(d.executed_buys) for d in self.days)
        total_sell_signals = sum(len(d.pending_sell_signals) for d in self.days)
        total_buy_signals = sum(len(d.pending_buy_signals) for d in self.days)
        final_asset = self.days[-1].total_asset
        total_return = (final_asset - self.config.initial_capital) / self.config.initial_capital

        print(f"\n{'═' * 60}")
        print(f"  回放汇总")
        print(f"{'═' * 60}")
        print(f"  区间: {self.config.start_date} → {self.config.end_date} "
              f"({len(self.days)} 个交易日)")
        print(f"  策略: {self.config.strategy_name}")
        print(f"  初始资金: {self.config.initial_capital:>14,.2f}")
        print(f"  最终资产: {final_asset:>14,.2f}")
        print(f"  总收益率: {total_return:>+13.2%}")
        print(f"  卖出信号: {total_sell_signals}  买入信号: {total_buy_signals}")
        print(f"  成交笔数: 卖出 {total_sells}  买入 {total_buys}")
        print(f"  最终持仓: {self.days[-1].holding_count} 只")
        if self.days[-1].portfolio:
            print(f"  持仓明细:")
            for p in self.days[-1].portfolio:
                pnl = (p["latest_price"] - p["cost_price"]) / p["cost_price"] if p["cost_price"] > 0 else 0
                print(f"    {p['ts_code']} {p['name']:　<6} "
                      f"{p['volume']:>6}股 成本:{p['cost_price']:.2f} "
                      f"现价:{p['latest_price']:.2f} ({pnl:+.2%})")
        print(f"{'═' * 60}")

        # 打印全部交易记录
        if self.broker.trade_log:
            print(f"\n  全部交易记录 ({len(self.broker.trade_log)} 笔):")
            print(f"  {'日期':<12} {'方向':<4} {'代码':<12} {'名称':<8} "
                  f"{'数量':>6} {'价格':>8} {'金额':>12} {'盈亏':>10}")
            print(f"  {'-' * 76}")
            for t in self.broker.trade_log:
                pnl_str = f"{t['pnl']:>+10,.2f}" if "pnl" in t else f"{'':>10}"
                print(f"  {t['date']:<12} {t['direction']:<4} {t['ts_code']:<12} "
                      f"{t.get('name',''):<8} {t['volume']:>6} "
                      f"{t['price']:>8.2f} {t['amount']:>12,.2f} {pnl_str}")
        print()

    def _save_results(self) -> None:
        output_dir = Path("data/replay")
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"replay_{self.config.strategy_name}_{timestamp}.json"
        filepath = output_dir / filename

        result = {
            "config": {
                "strategy_name": self.config.strategy_name,
                "start_date": self.config.start_date,
                "end_date": self.config.end_date,
                "initial_capital": self.config.initial_capital,
                "fund_ratio": self.config.fund_ratio,
                "buy_existing": self.config.buy_existing,
                "lookback_days": self.config.lookback_days,
                "live_start_date": self.config.live_start_date,
                "commission_rate": self.config.commission_rate,
                "stamp_duty_rate": self.config.stamp_duty_rate,
            },
            "summary": {
                "trading_days": len(self.days),
                "initial_capital": self.config.initial_capital,
                "final_asset": self.days[-1].total_asset if self.days else 0,
                "total_return": (
                    (self.days[-1].total_asset - self.config.initial_capital) / self.config.initial_capital
                    if self.days else 0
                ),
                "total_trades": len(self.broker.trade_log),
            },
            "trade_log": self.broker.trade_log,
            "daily_records": [
                {
                    "date": d.date,
                    "executed_sells": d.executed_sells,
                    "executed_buys": d.executed_buys,
                    "pending_sell_signals": d.pending_sell_signals,
                    "pending_buy_signals": d.pending_buy_signals,
                    "portfolio": d.portfolio,
                    "cash": d.cash,
                    "total_asset": d.total_asset,
                    "holding_count": d.holding_count,
                }
                for d in self.days
            ],
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已保存: {filepath}")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def cli_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="实盘历史回放模拟器 — 用历史数据模拟调度器的信号生成和 T+1 执行流程",
    )
    parser.add_argument("--strategy", required=True, help="策略名称")
    parser.add_argument("--start", required=True, help="回放起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="回放结束日期 (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=100_000, help="初始资金 (默认 100000)")
    parser.add_argument("--fund-ratio", type=float, default=1.0, help="资金比例 (默认 1.0)")
    parser.add_argument("--buy-existing", action="store_true", help="允许买入已持仓股票")
    parser.add_argument("--lookback", type=int, default=250, help="回看天数 (默认 250)")
    parser.add_argument("--live-start", default="", help="实盘起始日 (默认等于 --start)")
    parser.add_argument("--commission", type=float, default=0.0003, help="佣金率 (默认 0.0003)")
    parser.add_argument("--stamp-duty", type=float, default=0.001, help="印花税率 (默认 0.001)")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出每日详细信号")

    args = parser.parse_args(argv)

    config = ReplayConfig(
        strategy_name=args.strategy,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        fund_ratio=args.fund_ratio,
        buy_existing=args.buy_existing,
        lookback_days=args.lookback,
        live_start_date=args.live_start or args.start,
        commission_rate=args.commission,
        stamp_duty_rate=args.stamp_duty,
        verbose=args.verbose,
    )

    engine = ReplayEngine(config)
    engine.run()
