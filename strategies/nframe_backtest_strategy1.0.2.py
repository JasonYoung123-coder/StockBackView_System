from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.tushare_client import TushareClient
from app.strategy.base import BaseStrategy


class Strategy(BaseStrategy):
    """Nframe 选股回测策略 v1.0.2 — 全市场量能环境择时版。

    在 v1.0 基础上新增"方案D: 全市场量能择时"：
    - 每日计算全市场总成交额 / N 日均成交额 = 量能比
    - 量能比 ≥ 活跃阈值 → 满仓运行（max_holdings 只）
    - 量能比 < 清淡阈值 → 暂停建仓（仅管理已有持仓）
    - 介于两者之间 → 半仓运行

    原理：策略依赖"放量突破"模式，市场整体缩量时放量突破信号的
    可信度下降；数据在 market_data 中已有，无需额外 API 调用。
    """

    name = "Nframe选股回测策略v1.0.2量能择时"
    description = (
        "基于放量缩量回调模式选股（5日/4日），"
        "叠加全市场量能环境择时，缩量市暂停建仓。"
    )
    is_portfolio_strategy = True
    lookback_days = 120

    def __init__(
        self,
        max_holdings: int = 5,
        stop_loss_lookback: int = 10,
        tp_volume_lookback: int = 30,
        tp_profit_threshold: float = 99.0,
        tp_prev_day_threshold: float = 99.0,
        rebalance_days: int = 9,
        rebalance_min_return: float = 5.0,
        filter_amplitude_pct: float = 30.0,
        filter_turnover_pct: float = 30.0,
        pattern_surge_ratio: float = 2.0,
        pattern_min_pct_chg: float = 3.8,
        pattern_shrink_ratio: float = 0.5,
        pattern_max_up_days: int = 1,
        pattern_max_up_pct: float = 1.5,
        timing_vol_ma: int = 20,
        timing_vol_active: float = 0.8,
        timing_vol_inactive: float = 0.6,
    ) -> None:
        self.max_holdings = int(max_holdings)
        self.position_per_stock = 1.0 / max(self.max_holdings, 1)
        self.stop_loss_lookback = int(stop_loss_lookback)
        self.tp_volume_lookback = int(tp_volume_lookback)
        self.tp_profit_threshold = float(tp_profit_threshold)
        self.tp_prev_day_threshold = float(tp_prev_day_threshold)
        self.rebalance_days = int(rebalance_days)
        self.rebalance_min_return = float(rebalance_min_return) / 100.0
        self.filter_amplitude_pct = float(filter_amplitude_pct) / 100.0
        self.filter_turnover_pct = float(filter_turnover_pct)
        self.pattern_surge_ratio = float(pattern_surge_ratio)
        self.pattern_min_pct_chg = float(pattern_min_pct_chg)
        self.pattern_shrink_ratio = float(pattern_shrink_ratio)
        self.pattern_max_up_days = int(pattern_max_up_days)
        self.pattern_max_up_pct = float(pattern_max_up_pct)
        self.timing_vol_ma = int(timing_vol_ma)
        self.timing_vol_active = float(timing_vol_active)
        self.timing_vol_inactive = float(timing_vol_inactive)

    def get_config_schema(self) -> dict:
        return {
            "title": "Nframe v1.0.2 量能择时参数",
            "fields": [
                {"name": "max_holdings", "label": "满仓最大持股数", "type": "number", "min": 1, "max": 10, "step": 1, "default": self.max_holdings},
                {"name": "timing_vol_ma", "label": "择时-量能均线周期", "type": "number", "min": 5, "max": 60, "step": 1, "default": self.timing_vol_ma},
                {"name": "timing_vol_active", "label": "择时-活跃量能比阈值", "type": "number", "min": 0.5, "max": 1.5, "step": 0.05, "default": self.timing_vol_active},
                {"name": "timing_vol_inactive", "label": "择时-清淡量能比阈值", "type": "number", "min": 0.3, "max": 1.0, "step": 0.05, "default": self.timing_vol_inactive},
                {"name": "stop_loss_lookback", "label": "止损回看天数", "type": "number", "min": 3, "max": 50, "step": 1, "default": self.stop_loss_lookback},
                {"name": "tp_volume_lookback", "label": "止盈-成交量回看天数", "type": "number", "min": 5, "max": 60, "step": 1, "default": self.tp_volume_lookback},
                {"name": "tp_profit_threshold", "label": "止盈-盈利筹码占比阈值(%)", "type": "number", "min": 80, "max": 100, "step": 0.5, "default": self.tp_profit_threshold},
                {"name": "tp_prev_day_threshold", "label": "止盈-前日盈利筹码阈值(%)", "type": "number", "min": 80, "max": 100, "step": 0.5, "default": self.tp_prev_day_threshold},
                {"name": "rebalance_days", "label": "调仓持仓天数", "type": "number", "min": 3, "max": 30, "step": 1, "default": self.rebalance_days},
                {"name": "rebalance_min_return", "label": "调仓最低收益(%)", "type": "number", "min": 0, "max": 50, "step": 0.5, "default": round(self.rebalance_min_return * 100, 1)},
                {"name": "filter_amplitude_pct", "label": "振幅过滤阈值(%)", "type": "number", "min": 5, "max": 100, "step": 1, "default": round(self.filter_amplitude_pct * 100)},
                {"name": "filter_turnover_pct", "label": "10日累计换手率过滤阈值(%)", "type": "number", "min": 5, "max": 200, "step": 5, "default": self.filter_turnover_pct},
                {"name": "pattern_surge_ratio", "label": "放量倍数", "type": "number", "min": 1.0, "max": 5.0, "step": 0.1, "default": self.pattern_surge_ratio},
                {"name": "pattern_min_pct_chg", "label": "放量日最小涨幅(%)", "type": "number", "min": 1.0, "max": 10.0, "step": 0.1, "default": self.pattern_min_pct_chg},
                {"name": "pattern_shrink_ratio", "label": "T+1缩量比例上限", "type": "number", "min": 0.2, "max": 0.9, "step": 0.05, "default": self.pattern_shrink_ratio},
                {"name": "pattern_max_up_days", "label": "回调期允许上涨天数", "type": "number", "min": 0, "max": 3, "step": 1, "default": self.pattern_max_up_days},
                {"name": "pattern_max_up_pct", "label": "上涨日最大涨幅(%)", "type": "number", "min": 0.5, "max": 5.0, "step": 0.1, "default": self.pattern_max_up_pct},
            ],
        }

    # ═══════════════════════════ 主入口 ═══════════════════════════

    def generate_portfolio_weights(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
    ) -> tuple[pd.DataFrame, dict]:
        if market_data.empty or not trade_dates:
            return pd.DataFrame(), {}

        self.report_progress(50, "正在预处理市场数据")
        frame = market_data.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

        if "name" in frame.columns:
            frame = frame[~frame["name"].str.contains("ST", na=False)].copy()

        for col in ("close", "high", "low", "open", "vol", "pct_chg"):
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")

        self.report_progress(52, "正在预计算指标")
        self._compute_indicators(frame)

        # ── 预计算全市场量能比（方案D核心） ──
        self.report_progress(54, "正在计算全市场量能环境")
        market_vol_ratio = self._compute_market_volume_ratio(frame, self.timing_vol_ma)

        self.report_progress(55, "正在预计算模式信号")
        stock_groups: dict[str, pd.DataFrame] = {
            str(ts_code): grp.reset_index(drop=True)
            for ts_code, grp in frame.groupby("ts_code", sort=False)
        }
        pattern_5d, pattern_4d = self._precompute_patterns(stock_groups)

        self.report_progress(58, "正在构建查询索引")
        stock_cache = self._build_stock_cache(stock_groups)
        daily_candidates = self._build_daily_candidate_index(pattern_5d, pattern_4d)

        trade_index = [pd.Timestamp(d) for d in trade_dates]
        all_stocks = sorted(frame["ts_code"].unique())

        col_idx_map = {c: i for i, c in enumerate(all_stocks)}
        weights_np = np.zeros((len(trade_index), len(all_stocks)), dtype=np.float64)

        self._tushare = TushareClient()
        self._chips_cache: dict[str, pd.DataFrame] = {}
        self._chips_cached_dates: dict[str, set[pd.Timestamp]] = {}
        self._chips_missing_attempts: dict[str, set[str]] = {}
        self._trade_dates_list = trade_index
        self._date_to_list_idx = {d: i for i, d in enumerate(trade_index)}
        self._warnings: list[str] = []

        holdings: dict[str, dict] = {}
        sell_reasons: dict[str, str] = {}
        sell_prices: dict[str, float] = {}
        sell_orders: dict[str, dict[str, object]] = {}
        buy_prices: dict[str, float] = {}
        rebalance_count = 0
        total_days = len(trade_index)
        pos_wt = self.position_per_stock

        for day_idx, trade_date in enumerate(trade_index):
            if day_idx % 50 == 0:
                self.report_progress(
                    60 + (day_idx / max(total_days, 1)) * 35,
                    f"回测进度 {day_idx + 1}/{total_days}",
                )

            td_np = np.datetime64(trade_date)

            for info in holdings.values():
                if trade_date > info["buy_date"]:
                    info["days_held"] = info.get("days_held", 0) + 1

            # ══════ 0. 量能择时信号 ══════
            mvr = market_vol_ratio.get(td_np, 1.0)
            if mvr < self.timing_vol_inactive:
                effective_max = 0
            elif mvr < self.timing_vol_active:
                effective_max = max(1, self.max_holdings // 2)
            else:
                effective_max = self.max_holdings

            sold_today: set[str] = set()

            # ══════ 1. 止损 / 止盈 / 调仓 ══════
            for ts_code in list(holdings.keys()):
                info = holdings[ts_code]
                if info.get("days_held", 0) < 1:
                    continue

                sc = stock_cache.get(ts_code)
                if sc is None:
                    continue
                ri = sc["date_idx"].get(td_np)
                if ri is None or ri < 1:
                    continue

                cur_close = sc["close"][ri]
                cur_vol = sc["vol"][ri]
                sig = f"{ts_code}|{trade_date.strftime('%Y-%m-%d')}"

                # ── 止损 ──
                sl_start = max(0, ri - self.stop_loss_lookback)
                if ri - sl_start >= self.stop_loss_lookback:
                    if cur_close < float(np.nanmin(sc["low"][sl_start:ri])):
                        sell_reasons[sig] = "止损-收盘价破近10日最低点"
                        holdings.pop(ts_code)
                        self._cleanup_chips(ts_code)
                        sold_today.add(ts_code)
                        rebalance_count += 1
                        continue

                # ── 止盈 ──
                tp_start = max(0, ri + 1 - self.tp_volume_lookback)
                if ri - tp_start + 1 >= self.tp_volume_lookback:
                    if cur_vol >= float(np.nanmax(sc["vol"][tp_start : ri + 1])):
                        chip_price = float(sc["close_unadj"][ri])
                        self._ensure_chips(ts_code, trade_date)

                        ratio = self._get_profit_ratio(ts_code, trade_date, chip_price)
                        if ratio is not None and ratio > self.tp_profit_threshold:
                            sell_reasons[sig] = "止盈-筹码获利盘过高"
                            sell_prices[sig] = cur_close - 0.01
                            holdings.pop(ts_code)
                            self._cleanup_chips(ts_code)
                            sold_today.add(ts_code)
                            rebalance_count += 1
                            continue

                        prev_td = self._get_prev_trade_date(trade_date)
                        if prev_td is not None and ri >= 1:
                            prev_ratio = self._get_profit_ratio(
                                ts_code, prev_td, float(sc["close_unadj"][ri - 1]),
                            )
                            if prev_ratio is not None and prev_ratio > self.tp_prev_day_threshold:
                                sell_reasons[sig] = "止盈-前日筹码获利盘过高"
                                sell_prices[sig] = cur_close - 0.01
                                holdings.pop(ts_code)
                                self._cleanup_chips(ts_code)
                                sold_today.add(ts_code)
                                rebalance_count += 1
                                continue

                # ── 调仓 ──
                dh = info.get("days_held", 0)
                if dh >= self.rebalance_days:
                    bp = info["buy_price"]
                    ret = (cur_close / bp - 1.0) if bp > 0 else 0.0
                    if ret < self.rebalance_min_return:
                        sell_reasons[sig] = f"调仓-持仓{dh}日收益{ret:.1%}<{self.rebalance_min_return:.0%}"
                        holdings.pop(ts_code)
                        self._cleanup_chips(ts_code)
                        sold_today.add(ts_code)
                        rebalance_count += 1
                        continue

            # ══════ 2. 选股买入（受量能择时限制） ══════
            available = effective_max - len(holdings)
            if available > 0:
                excluded = set(holdings.keys()) | sold_today
                candidates = self._find_candidates(td_np, daily_candidates, stock_cache, excluded)
                for cand in candidates[:available]:
                    ts_code = cand["ts_code"]
                    sc = stock_cache.get(ts_code)
                    ri = sc["date_idx"].get(td_np) if sc else None
                    ep = self._get_next_open_fast(sc, ri)
                    if ep is None:
                        ep = cand["close_price"]
                    holdings[ts_code] = {
                        "buy_date": trade_date,
                        "buy_price": ep,
                        "weight": pos_wt,
                    }
                    rebalance_count += 1

            # ══════ 3. 权重写入 ══════
            for ts_code in holdings:
                ci = col_idx_map.get(ts_code)
                if ci is not None:
                    weights_np[day_idx, ci] = pos_wt

        weights = pd.DataFrame(weights_np, index=pd.DatetimeIndex(trade_index), columns=all_stocks)

        latest_holdings = [
            {"ts_code": tc, "buy_date": h["buy_date"].strftime("%Y-%m-%d"), "days_held": h.get("days_held", 0)}
            for tc, h in holdings.items()
        ]
        return weights, {
            "rebalance_count": rebalance_count,
            "latest_holdings": latest_holdings,
            "sell_reasons": sell_reasons,
            "sell_prices": sell_prices,
            "sell_orders": sell_orders,
            "buy_prices": buy_prices,
            "warnings": self._warnings,
        }

    # ═══════════════════════ 量能择时 ═══════════════════════

    @staticmethod
    def _compute_market_volume_ratio(
        frame: pd.DataFrame,
        ma_period: int,
    ) -> dict[np.datetime64, float]:
        """计算每个交易日的全市场量能比 = 当日总成交额 / N日均成交额。

        优先使用 amount（成交额），退回 vol（成交量）。
        返回 {numpy.datetime64: float} 字典供主循环 O(1) 查询。
        """
        vol_col = "amount" if "amount" in frame.columns else "vol"
        daily_total = (
            frame.groupby("trade_date")[vol_col]
            .sum()
            .sort_index()
        )
        daily_ma = daily_total.rolling(ma_period, min_periods=ma_period).mean()
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = daily_total / daily_ma

        result: dict[np.datetime64, float] = {}
        for dt, val in ratio.items():
            result[np.datetime64(dt)] = float(val) if pd.notna(val) else 1.0
        return result

    # ═══════════════════════ 指标预计算 ═══════════════════════

    @staticmethod
    def _compute_indicators(frame: pd.DataFrame) -> None:
        ts_col = frame["ts_code"]

        def _gt(col, func):
            return frame.groupby(ts_col, sort=False)[col].transform(func)

        _low_n = _gt("low", lambda s: s.rolling(9, min_periods=1).min())
        _high_n = _gt("high", lambda s: s.rolling(9, min_periods=1).max())
        _denom = (_high_n - _low_n).replace(0.0, pd.NA)
        _rsv = ((frame["close"] - _low_n) / _denom * 100).fillna(0.0)
        _k = _rsv.groupby(ts_col, sort=False).transform(lambda s: s.ewm(com=2, adjust=False).mean())
        _d = _k.groupby(ts_col, sort=False).transform(lambda s: s.ewm(com=2, adjust=False).mean())
        frame["kdj_j"] = 3 * _k - 2 * _d

        max_h_10 = _gt("high", lambda s: s.rolling(10, min_periods=10).max())
        min_l_10 = _gt("low", lambda s: s.rolling(10, min_periods=10).min())
        with np.errstate(divide="ignore", invalid="ignore"):
            frame["amp_10d"] = np.where(min_l_10 > 0, (max_h_10 - min_l_10) / min_l_10, np.inf)

        if "turnover_rate" in frame.columns:
            frame["tr_10d"] = _gt("turnover_rate", lambda s: s.rolling(10, min_periods=10).sum())
        else:
            frame["tr_10d"] = 0.0

    # ═══════════════════════ 模式预计算 ═══════════════════════

    def _precompute_patterns(self, stock_groups):
        pattern_5d: dict[str, dict] = {}
        pattern_4d: dict[str, dict] = {}
        sample_group = next(iter(stock_groups.values()), pd.DataFrame())
        close_col = "close_unadj" if "close_unadj" in sample_group.columns else "close"
        surge = self.pattern_surge_ratio
        min_pct = self.pattern_min_pct_chg
        total = len(stock_groups)
        for prog_idx, (ts_code, group) in enumerate(stock_groups.items()):
            if prog_idx % 500 == 0:
                self.report_progress(55 + (prog_idx / max(total, 1)) * 3, f"预计算模式 {prog_idx}/{total}")
            n = len(group)
            if n < 5:
                continue
            closes = group[close_col].values.astype(float)
            vols = group["vol"].values.astype(float)
            pct_chgs = group["pct_chg"].values.astype(float)
            dates = group["trade_date"].values
            prev_vols = np.empty(n); prev_vols[0] = 0.0; prev_vols[1:] = vols[:-1]
            with np.errstate(divide="ignore", invalid="ignore"):
                vol_ratio = np.where(prev_vols > 0, vols / prev_vols, 0.0)
            surge_mask = (vol_ratio > surge) & (pct_chgs >= min_pct) & np.isfinite(vol_ratio) & np.isfinite(pct_chgs)
            surge_indices = np.where(surge_mask)[0]
            d5: dict = {}
            d4: dict = {}
            for si in surge_indices:
                if si + 4 < n:
                    detail = self._check_pattern_at(closes, vols, pct_chgs, si + 4, 5)
                    if detail is not None:
                        d5[dates[si + 4]] = detail
                if si + 3 < n:
                    detail = self._check_pattern_at(closes, vols, pct_chgs, si + 3, 4)
                    if detail is not None:
                        d4[dates[si + 3]] = detail
            if d5:
                pattern_5d[ts_code] = d5
            if d4:
                pattern_4d[ts_code] = d4
        return pattern_5d, pattern_4d

    def _check_pattern_at(self, closes, vols, pct_chgs, end_idx, pattern_days):
        start_idx = end_idx - pattern_days + 1
        if start_idx < 1:
            return None
        first_vol = vols[start_idx]
        prev_vol = vols[start_idx - 1]
        if np.isnan(first_vol) or np.isnan(prev_vol) or prev_vol <= 0 or first_vol / prev_vol <= self.pattern_surge_ratio:
            return None
        day1_pct = pct_chgs[start_idx]
        if np.isnan(day1_pct) or day1_pct < self.pattern_min_pct_chg:
            return None
        up_days = 0
        for i in range(start_idx + 1, end_idx + 1):
            cv, pv = vols[i], vols[i - 1]
            if np.isnan(cv) or np.isnan(pv) or pv <= 0 or cv >= pv:
                return None
            cc, pc = closes[i], closes[i - 1]
            if np.isnan(cc) or np.isnan(pc):
                return None
            if cc >= pc:
                up_days += 1
                if up_days > self.pattern_max_up_days:
                    return None
                if not np.isnan(pct_chgs[i]) and pct_chgs[i] >= self.pattern_max_up_pct:
                    return None
        day2_vol = vols[start_idx + 1]
        day2_shrink = day2_vol / first_vol
        if np.isnan(day2_vol) or first_vol <= 0 or day2_shrink >= self.pattern_shrink_ratio:
            return None
        return {
            "day1_pct_chg": float(day1_pct),
            "day1_vol_ratio": float(first_vol / prev_vol),
            "day2_shrink_ratio": float(day2_shrink),
        }

    # ═══════════════════════ 高性能索引 ═══════════════════════

    @staticmethod
    def _build_stock_cache(stock_groups):
        cache: dict[str, dict] = {}
        for ts_code, group in stock_groups.items():
            n = len(group)
            dates = group["trade_date"].values
            cache[ts_code] = {
                "date_idx": dict(zip(dates, range(n))),
                "n": n,
                "close": group["close"].values.astype(float),
                "high": group["high"].values.astype(float),
                "low": group["low"].values.astype(float),
                "open": group["open"].values.astype(float) if "open" in group.columns else np.full(n, np.nan),
                "vol": group["vol"].values.astype(float),
                "kdj_j": group["kdj_j"].values.astype(float) if "kdj_j" in group.columns else np.full(n, np.nan),
                "close_unadj": group["close_unadj"].values.astype(float) if "close_unadj" in group.columns else group["close"].values.astype(float),
                "amp_10d": group["amp_10d"].values.astype(float) if "amp_10d" in group.columns else np.full(n, np.inf),
                "tr_10d": group["tr_10d"].values.astype(float) if "tr_10d" in group.columns else np.zeros(n),
            }
        return cache

    @staticmethod
    def _build_daily_candidate_index(pattern_5d, pattern_4d):
        index: dict = {}
        seen_5d: dict = {}
        for ts_code, dd in pattern_5d.items():
            for dt, detail in dd.items():
                index.setdefault(dt, []).append((ts_code, detail, "5日"))
                seen_5d.setdefault(dt, set()).add(ts_code)
        for ts_code, dd in pattern_4d.items():
            for dt, detail in dd.items():
                if ts_code not in seen_5d.get(dt, set()):
                    index.setdefault(dt, []).append((ts_code, detail, "4日"))
        return index

    # ═══════════════════════ 候选股筛选 ═══════════════════════

    def _find_candidates(self, td_np, daily_candidates, stock_cache, excluded):
        matches = daily_candidates.get(td_np)
        if not matches:
            return []
        candidates = []
        for ts_code, detail, pattern in matches:
            if ts_code in excluded:
                continue
            sc = stock_cache.get(ts_code)
            if sc is None:
                continue
            ri = sc["date_idx"].get(td_np)
            if ri is None or ri < 9:
                continue
            amp = sc["amp_10d"][ri]
            if np.isnan(amp) or amp > self.filter_amplitude_pct:
                continue
            tr = sc["tr_10d"][ri]
            if not np.isnan(tr) and tr > self.filter_turnover_pct:
                continue
            cp = sc["close"][ri]
            if np.isnan(cp) or cp <= 0:
                continue
            score = self._score_candidate(detail, sc["kdj_j"][ri])
            candidates.append({"ts_code": ts_code, "close_price": float(cp), "score": score, "pattern": pattern})
        candidates.sort(key=lambda x: (-x["score"], x["pattern"] != "5日", x["close_price"]))
        return candidates

    @staticmethod
    def _score_candidate(detail, kdj_j):
        score = 0
        shrink = detail["day2_shrink_ratio"]
        if shrink < 0.30:
            score += 3
        elif shrink < 0.40:
            score += 2
        elif shrink < 0.50:
            score += 1
        if not np.isnan(kdj_j):
            if kdj_j < 40:
                score += 2
            elif kdj_j < 55:
                score += 1
        pct = detail["day1_pct_chg"]
        if pct > 9.5:
            score += 2
        elif pct > 5.0:
            score += 1
        vol_ratio = detail["day1_vol_ratio"]
        if vol_ratio > 5.0:
            score += 4
        elif vol_ratio > 4.0:
            score += 3
        elif vol_ratio > 3.0:
            score += 2
        elif vol_ratio > 2.5:
            score += 1
        return score

    # ═══════════════════════ 筹码数据 ═══════════════════════

    def _ensure_chips(self, ts_code, trade_date):
        required = [pd.Timestamp(trade_date).normalize()]
        prev_td = self._get_prev_trade_date(trade_date)
        if prev_td is not None:
            required.append(pd.Timestamp(prev_td).normalize())
        cached = self._chips_cached_dates.get(ts_code, set())
        missing = [dt for dt in required if dt not in cached]
        if not missing:
            return
        attempted = self._chips_missing_attempts.setdefault(ts_code, set())
        pending = [dt for dt in missing if dt.strftime("%Y%m%d") not in attempted]
        if not pending:
            return
        s_str = min(pending).strftime("%Y%m%d")
        e_str = max(pending).strftime("%Y%m%d")
        try:
            new_data = self._tushare.get_cyq_chips_range(ts_code, s_str, e_str)
        except Exception:
            for dt in pending:
                attempted.add(dt.strftime("%Y%m%d"))
            return
        if new_data is not None and not new_data.empty:
            existing = self._chips_cache.get(ts_code)
            if existing is not None and not existing.empty:
                self._chips_cache[ts_code] = (
                    pd.concat([existing, new_data], ignore_index=True)
                    .drop_duplicates(subset=["trade_date", "price"], keep="last")
                )
            else:
                self._chips_cache[ts_code] = new_data
            new_dates = set(pd.to_datetime(new_data["trade_date"]).dt.normalize())
            cached.update(new_dates)
            self._chips_cached_dates[ts_code] = cached
        for dt in pending:
            if dt not in self._chips_cached_dates.get(ts_code, set()):
                attempted.add(dt.strftime("%Y%m%d"))

    def _get_profit_ratio(self, ts_code, trade_date, close_price):
        chips = self._chips_cache.get(ts_code)
        if chips is None or chips.empty:
            return None
        td = pd.Timestamp(trade_date)
        day_chips = chips.loc[chips["trade_date"] == td]
        if day_chips.empty:
            return None
        ref_price = close_price
        prices = pd.to_numeric(day_chips["price"], errors="coerce").dropna()
        if not prices.empty:
            above = prices.loc[prices > close_price]
            if not above.empty:
                nxt = float(above.min())
                if close_price > 0 and (nxt - close_price) / close_price < 0.001:
                    ref_price = nxt
        return float(day_chips.loc[day_chips["price"] <= ref_price, "percent"].sum())

    def _get_prev_trade_date(self, trade_date):
        idx = self._date_to_list_idx.get(pd.Timestamp(trade_date))
        if idx is None or idx <= 0:
            return None
        return self._trade_dates_list[idx - 1]

    def _cleanup_chips(self, ts_code):
        self._chips_cache.pop(ts_code, None)
        self._chips_cached_dates.pop(ts_code, None)
        self._chips_missing_attempts.pop(ts_code, None)

    @staticmethod
    def _get_next_open_fast(sc, row_idx):
        if sc is None or row_idx is None or row_idx + 1 >= sc["n"]:
            return None
        val = sc["open"][row_idx + 1]
        if np.isnan(val) or val <= 0:
            return None
        return float(val)
