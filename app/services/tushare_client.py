from __future__ import annotations

from pathlib import Path
import time

import pandas as pd
import tushare as ts

from app.core.config import get_settings, require_tushare_token


class TushareClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None
        self._chips_decisions: list[dict[str, object]] = []

    @property
    def client(self):
        if self._client is None:
            self._client = ts.pro_api(require_tushare_token())
        return self._client

    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._get_timeseries("stock_daily", ts_code, start_date, end_date)

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._get_timeseries("index_daily", ts_code, start_date, end_date)

    def get_fund_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._get_timeseries("fund_daily", ts_code, start_date, end_date)

    def get_trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        cache_file = self.settings.cache_dir / "calendar" / "trade_calendar.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        cached = pd.DataFrame()
        if cache_file.exists():
            cached = pd.read_csv(cache_file)
            if not cached.empty:
                cached["cal_date"] = pd.to_datetime(cached["cal_date"])

        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        requested = pd.DataFrame()
        if not cached.empty:
            mask = (cached["cal_date"] >= start_dt) & (cached["cal_date"] <= end_dt)
            requested = cached.loc[mask].copy()
        if not requested.empty and requested["cal_date"].min() <= start_dt and requested["cal_date"].max() >= end_dt:
            return requested.sort_values("cal_date").reset_index(drop=True)

        frame = self.client.trade_cal(
            exchange="SSE",
            start_date=start_date,
            end_date=end_date,
            fields="cal_date,is_open,pretrade_date",
        )
        if frame is None or frame.empty:
            raise RuntimeError("未获取到交易日历数据。")
        frame["cal_date"] = pd.to_datetime(frame["cal_date"])
        merged = pd.concat([cached, frame], ignore_index=True) if not cached.empty else frame
        merged = merged.drop_duplicates(subset=["cal_date"], keep="last").sort_values("cal_date")
        merged.to_csv(cache_file, index=False)
        mask = (merged["cal_date"] >= start_dt) & (merged["cal_date"] <= end_dt)
        return merged.loc[mask].reset_index(drop=True)

    def get_stock_basic(self) -> pd.DataFrame:
        cache_file = self.settings.cache_dir / "reference" / "stock_basic.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if cache_file.exists():
            frame = pd.read_csv(cache_file)
            if not frame.empty:
                return frame

        frame = self._call_with_retry(
            self.client.stock_basic,
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,list_date",
        )
        if frame is None or frame.empty:
            raise RuntimeError("未获取到股票列表。")
        frame.to_csv(cache_file, index=False)
        return frame

    def get_market_daily_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return self._get_snapshot(
            kind="market_daily",
            trade_date=trade_date,
            fields="ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount",
            fetcher=self.client.daily,
        )

    def get_daily_basic_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return self._get_snapshot(
            kind="daily_basic",
            trade_date=trade_date,
            fields="ts_code,trade_date,turnover_rate,circ_mv",
            fetcher=self.client.daily_basic,
        )

    def get_moneyflow_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return self._get_snapshot(
            kind="moneyflow",
            trade_date=trade_date,
            fields="ts_code,trade_date,net_mf_amount",
            fetcher=self.client.moneyflow,
        )

    def get_adj_factor_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return self._get_snapshot(
            kind="adj_factor",
            trade_date=trade_date,
            fields="ts_code,trade_date,adj_factor",
            fetcher=self.client.adj_factor,
        )

    def get_moneyflow_ths_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return self._get_snapshot(
            kind="moneyflow_ths",
            trade_date=trade_date,
            fields="ts_code,trade_date,name,net_amount",
            fetcher=self.client.moneyflow_ths,
        )

    def get_moneyflow_cnt_ths_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return self._get_snapshot(
            kind="moneyflow_cnt_ths",
            trade_date=trade_date,
            fields="ts_code,trade_date,name,net_amount",
            fetcher=self.client.moneyflow_cnt_ths,
        )

    def get_ths_member(self, ts_code: str) -> pd.DataFrame:
        cache_file = self.settings.cache_dir / "ths_member" / f"{ts_code.replace('.', '_')}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if cache_file.exists():
            cached = pd.read_csv(cache_file)
            if not cached.empty:
                return cached
        frame = self._call_with_retry(
            self.client.ths_member, ts_code=ts_code,
            fields="ts_code,code,name,is_new",
        )
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["ts_code", "code", "name", "is_new"])
        frame.to_csv(cache_file, index=False)
        return frame

    def get_ths_member_by_stock(self, con_code: str) -> pd.DataFrame:
        cache_file = self.settings.cache_dir / "ths_member_stock" / f"{con_code.replace('.', '_')}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if cache_file.exists():
            cached = pd.read_csv(cache_file)
            if not cached.empty:
                return cached
        frame = self._call_with_retry(
            self.client.ths_member, con_code=con_code,
            fields="ts_code,code,name,is_new",
        )
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["ts_code", "code", "name", "is_new"])
        frame.to_csv(cache_file, index=False)
        return frame

    def get_idx_factor_pro(
        self, ts_code: str, start_date: str, end_date: str,
        fields: str = "ts_code,trade_date,close,ma_bfq_250,macd_dif_bfq",
    ) -> pd.DataFrame:
        return self._get_factor_timeseries("idx_factor_pro", ts_code, start_date, end_date, fields)

    def get_stk_factor_pro(
        self, ts_code: str, start_date: str, end_date: str,
        fields: str = "ts_code,trade_date,close,open,ma_bfq_250,kdj_bfq",
    ) -> pd.DataFrame:
        return self._get_factor_timeseries("stk_factor_pro", ts_code, start_date, end_date, fields)

    def _get_factor_timeseries(
        self, kind: str, ts_code: str, start_date: str, end_date: str, fields: str,
    ) -> pd.DataFrame:
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        cache_file = self._cache_file(kind, ts_code)
        cached = self._load_cache(cache_file)
        requested = self._slice_date_range(cached, start_date, end_date)

        requested_fields = [f.strip() for f in fields.split(",") if f.strip()] if fields else []
        fields_ok = (
            not requested.empty
            and all(f in requested.columns for f in requested_fields)
        ) if requested_fields else not requested.empty

        if (
            fields_ok
            and requested["trade_date"].min() <= start_dt
            and requested["trade_date"].max() >= end_dt
        ):
            return requested

        fetcher = getattr(self.client, kind)
        try:
            fresh = self._call_with_retry(
                fetcher, ts_code=ts_code, start_date=start_date, end_date=end_date,
                fields=fields,
            )
        except Exception:
            if not requested.empty:
                return requested
            raise
        if fresh is None or fresh.empty:
            if not requested.empty:
                return requested
            return pd.DataFrame()

        fresh = self._normalize_timeseries(fresh)
        merged = self._merge_cache(cached, fresh)
        self._save_cache(cache_file, merged)
        return self._slice_date_range(merged, start_date, end_date)

    def get_cyq_chips_range(
        self, ts_code: str, start_date: str, end_date: str, *, skip_cache: bool = False,
    ) -> pd.DataFrame:
        decision: dict[str, object] = {
            "ts_code": ts_code,
            "requested_range": f"{start_date}~{end_date}",
            "preferred_source": "tushare",
            "priority_order": ["tushare"],
            "attempts": [],
        }
        try:
            frame, attempt = self._get_cyq_chips_range_from_source(
                "tushare", ts_code, start_date, end_date, skip_cache=skip_cache,
            )
        except Exception as exc:
            decision["attempts"].append(
                {
                    "source": "tushare",
                    "status": "request_failed",
                    "error": str(exc),
                }
            )
            decision["selected_source"] = None
            decision["selected_status"] = "all_failed"
            decision["selection_reason"] = "tushare_request_failed"
            self._chips_decisions.append(decision)
            raise RuntimeError(f"筹码数据获取失败，仅尝试 tushare: {exc}") from exc
        decision["attempts"].append(attempt)
        if frame is not None and not frame.empty:
            decision["selected_source"] = "tushare"
            decision["selected_status"] = attempt["status"]
            decision["selection_reason"] = self._build_selection_reason(decision["attempts"], "tushare", "tushare")
            self._chips_decisions.append(decision)
            return frame
        decision["selected_source"] = None
        decision["selected_status"] = "no_data"
        decision["selection_reason"] = "tushare_returned_empty"
        self._chips_decisions.append(decision)
        return self._empty_cyq_frame()

    def get_chips_data_source_report(self) -> dict[str, object]:
        selected_source_counts = {"tushare": 0}
        selected_status_counts: dict[str, int] = {}

        for item in self._chips_decisions:
            selected_source = item.get("selected_source")
            if selected_source == "tushare":
                selected_source_counts[selected_source] += 1

            selected_status = str(item.get("selected_status", "unknown"))
            selected_status_counts[selected_status] = selected_status_counts.get(selected_status, 0) + 1

        return {
            "preferred_source": "tushare",
            "priority_order": ["tushare"],
            "total_requests": len(self._chips_decisions),
            "selected_source_counts": selected_source_counts,
            "selected_status_counts": selected_status_counts,
            "fallback_cases": [],
        }

    def _get_cyq_chips_range_from_source(
        self,
        source: str,
        ts_code: str,
        start_date: str,
        end_date: str,
        *,
        skip_cache: bool = False,
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        cache_file = self._cache_file(f"cyq_chips_{source}", ts_code)
        cached = self._load_cache(cache_file)
        requested = self._slice_date_range(cached, start_date, end_date)
        cache_coverage = self._describe_trade_date_coverage(requested)

        if (
            not skip_cache
            and not requested.empty
            and requested["trade_date"].min() <= start_dt
            and requested["trade_date"].max() >= end_dt
        ):
            return requested, {
                "source": source,
                "status": "cache_hit",
                "cache_coverage": cache_coverage,
                "rows": int(len(requested)),
            }

        try:
            if source == "tushare":
                fresh = self._call_with_retry(
                    self.client.cyq_chips,
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                )
                merge_subset = ["trade_date", "price"]
            else:
                raise RuntimeError(f"不支持的筹码数据源: {source}")
        except Exception as exc:
            coverage = self._describe_trade_date_coverage(requested)
            if coverage:
                raise RuntimeError(
                    f"{source} 筹码数据拉取失败，当前仅使用本地缓存覆盖区间 {coverage}，"
                    f"无法确认是否完整覆盖请求区间 {start_date}~{end_date}: {exc}"
                ) from exc
            raise RuntimeError(
                f"{source} 筹码数据拉取失败，且本地缓存无可用覆盖区间，请求区间 {start_date}~{end_date}: {exc}"
            ) from exc
        if fresh is None or fresh.empty:
            coverage = self._describe_trade_date_coverage(requested)
            if coverage:
                return self._empty_cyq_frame(), {
                    "source": source,
                    "status": "partial_cache_only",
                    "cache_coverage": coverage,
                    "rows": int(len(requested)),
                }
            return self._empty_cyq_frame(), {
                "source": source,
                "status": "empty_result",
                "cache_coverage": None,
                "rows": 0,
            }

        fresh = self._normalize_timeseries(fresh)
        merged = self._merge_cache(cached, fresh, subset=merge_subset)
        self._save_cache(cache_file, merged)
        result = self._slice_date_range(merged, start_date, end_date)
        return result, {
            "source": source,
            "status": "remote_fetch",
            "cache_coverage": cache_coverage,
            "rows": int(len(result)),
        }

    def _empty_cyq_frame(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["ts_code", "trade_date", "price", "percent"])

    def _build_selection_reason(
        self,
        attempts: list[dict[str, object]],
        preferred_source: str,
        selected_source: str,
    ) -> str:
        if selected_source == preferred_source:
            selected_attempt = attempts[-1] if attempts else {}
            selected_status = selected_attempt.get("status")
            if selected_status == "cache_hit":
                return "preferred_source_cache_hit"
            if selected_status == "remote_fetch":
                return "preferred_source_remote_success"
            return f"preferred_source_{selected_status}"

        preferred_attempt = attempts[0] if attempts else {}
        preferred_status = preferred_attempt.get("status")
        selected_attempt = attempts[-1] if attempts else {}
        selected_status = selected_attempt.get("status")
        return f"fallback_to_{selected_source}_after_{preferred_source}_{preferred_status}_via_{selected_status}"

    def _describe_trade_date_coverage(self, frame: pd.DataFrame) -> str | None:
        if frame is None or frame.empty or "trade_date" not in frame.columns:
            return None
        trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
        if trade_dates.empty:
            return None
        return f"{trade_dates.min().strftime('%Y-%m-%d')}~{trade_dates.max().strftime('%Y-%m-%d')}"

    def _get_timeseries(self, kind: str, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        cache_file = self._cache_file(kind, ts_code)
        cached = self._load_cache(cache_file)
        requested = self._slice_date_range(cached, start_date, end_date)
        if not requested.empty and requested["trade_date"].min() <= start_dt and requested["trade_date"].max() >= end_dt:
            return requested

        if kind == "stock_daily":
            fetcher = self.client.daily
        elif kind == "fund_daily":
            fetcher = self.client.fund_daily
        else:
            fetcher = self.client.index_daily
        try:
            fresh = self._call_with_retry(
                fetcher,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception:
            if not requested.empty:
                return requested
            raise
        if fresh is None or fresh.empty:
            raise RuntimeError(f"未获取到 {ts_code} 的区间数据。")

        fresh = self._normalize_timeseries(fresh)
        merged = self._merge_cache(cached, fresh)
        self._save_cache(cache_file, merged)
        return self._slice_date_range(merged, start_date, end_date)

    def _cache_file(self, kind: str, ts_code: str) -> Path:
        safe_code = ts_code.replace(".", "_")
        path = self.settings.cache_dir / kind / f"{safe_code}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _snapshot_cache_file(self, kind: str, trade_date: str) -> Path:
        path = self.settings.cache_dir / kind / f"{trade_date}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _get_snapshot(self, kind: str, trade_date: str, fields: str, fetcher) -> pd.DataFrame:
        cache_file = self._snapshot_cache_file(kind, trade_date)
        requested_columns = fields.split(",")
        if cache_file.exists():
            cached = self._normalize_timeseries(pd.read_csv(cache_file))
            if all(column in cached.columns for column in requested_columns):
                return cached
            try:
                frame = self._call_with_retry(fetcher, trade_date=trade_date, fields=fields)
            except Exception:
                return self._ensure_snapshot_columns(cached, requested_columns)
            if frame is None or frame.empty:
                return self._ensure_snapshot_columns(cached, requested_columns)
            frame = self._ensure_snapshot_columns(self._normalize_timeseries(frame), requested_columns)
            frame.to_csv(cache_file, index=False)
            return frame

        try:
            frame = self._call_with_retry(fetcher, trade_date=trade_date, fields=fields)
        except Exception:
            return pd.DataFrame(columns=requested_columns)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=requested_columns)
        frame = self._ensure_snapshot_columns(self._normalize_timeseries(frame), requested_columns)
        frame.to_csv(cache_file, index=False)
        return frame

    def _ensure_snapshot_columns(self, frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
        normalized = frame.copy()
        for column in columns:
            if column not in normalized.columns:
                normalized[column] = pd.NA
        return normalized[columns]

    def _call_with_retry(self, func, /, *args, **kwargs):
        last_error = None
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= 2:
                    break
                time.sleep(1.0 + attempt)
        raise last_error

    def _load_cache(self, cache_file: Path) -> pd.DataFrame:
        if not cache_file.exists():
            return pd.DataFrame()
        return self._normalize_timeseries(pd.read_csv(cache_file))

    def _save_cache(self, cache_file: Path, frame: pd.DataFrame) -> None:
        frame.to_csv(cache_file, index=False)

    def _merge_cache(self, cached: pd.DataFrame, fresh: pd.DataFrame, subset: list[str] | None = None) -> pd.DataFrame:
        if cached.empty:
            return fresh
        merged = pd.concat([cached, fresh], ignore_index=True)
        merged = merged.drop_duplicates(subset=subset or ["trade_date"], keep="last")
        return self._normalize_timeseries(merged)

    def _slice_date_range(self, frame: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
        if frame.empty:
            return frame
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        mask = (frame["trade_date"] >= start_dt) & (frame["trade_date"] <= end_dt)
        return self._normalize_timeseries(frame.loc[mask].copy())

    def _normalize_timeseries(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        normalized = frame.copy()
        if "trade_date" in normalized.columns:
            normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
            "turnover_rate",
            "circ_mv",
            "net_mf_amount",
            "adj_factor",
            "price",
            "percent",
        ]
        for column in numeric_columns:
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        if "trade_date" in normalized.columns:
            normalized = normalized.sort_values("trade_date").reset_index(drop=True)
        return normalized
