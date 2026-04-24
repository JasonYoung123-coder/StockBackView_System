from __future__ import annotations

import datetime
import logging
from threading import Lock

import pandas as pd

from app.services.tushare_client import TushareClient

logger = logging.getLogger(__name__)


class TradingCalendarError(RuntimeError):
    pass


class TradingCalendar:
    def __init__(self, client: TushareClient | None = None) -> None:
        self._client = client or TushareClient()
        self._open_set: frozenset[datetime.date] = frozenset()
        self._prev_map: dict[datetime.date, datetime.date] = {}
        self._next_map: dict[datetime.date, datetime.date] = {}
        self._range: tuple[datetime.date, datetime.date] | None = None

    def is_trading_day(self, date: datetime.date) -> bool:
        self._ensure_covers(date)
        return date in self._open_set

    def previous_trading_day(self, date: datetime.date) -> datetime.date:
        self._ensure_covers(date)
        if date in self._prev_map:
            return self._prev_map[date]
        raise TradingCalendarError(f"无前一交易日记录: {date}")

    def next_trading_day(self, date: datetime.date) -> datetime.date:
        self._ensure_covers(date)
        if date in self._next_map:
            return self._next_map[date]
        raise TradingCalendarError(f"无下一交易日记录: {date}")

    def refresh(self, year: int | None = None) -> None:
        target_year = year or datetime.date.today().year
        start = datetime.date(target_year, 1, 1)
        end = datetime.date(target_year + 1, 12, 31)
        self._load(start, end)

    # ────────────── internal ──────────────

    def _ensure_covers(self, date: datetime.date) -> None:
        if self._range is None:
            self.refresh()
            return
        lo, hi = self._range
        if date < lo or date > hi:
            self.refresh(year=date.year)

    def _load(self, start: datetime.date, end: datetime.date) -> None:
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")

        frame: pd.DataFrame | None = None
        try:
            frame = self._client.get_trade_calendar(s, e)
        except Exception as exc:
            logger.warning("Tushare 交易日历拉取失败,尝试使用 CSV 缓存: %s", exc)
            frame = self._read_cache_direct(start, end)
            if frame is None or frame.empty:
                raise TradingCalendarError(
                    f"无法获取交易日历 [{s}, {e}]:Tushare 不可达且无本地缓存"
                ) from exc

        self._build_maps(frame)
        self._range = (start, end)

    def _read_cache_direct(
        self, start: datetime.date, end: datetime.date
    ) -> pd.DataFrame | None:
        cache_file = self._client.settings.cache_dir / "calendar" / "trade_calendar.csv"
        if not cache_file.exists():
            return None
        try:
            df = pd.read_csv(cache_file)
        except Exception as exc:
            logger.warning("读取交易日历缓存失败: %s", exc)
            return None
        if df.empty or "cal_date" not in df.columns:
            return None
        df["cal_date"] = pd.to_datetime(df["cal_date"])
        start_dt = pd.to_datetime(start)
        end_dt = pd.to_datetime(end)
        mask = (df["cal_date"] >= start_dt) & (df["cal_date"] <= end_dt)
        return df.loc[mask].sort_values("cal_date").reset_index(drop=True)

    def _build_maps(self, frame: pd.DataFrame) -> None:
        df = frame.copy()
        df["cal_date"] = pd.to_datetime(df["cal_date"]).dt.date
        df = df.sort_values("cal_date").reset_index(drop=True)

        all_dates: list[datetime.date] = list(df["cal_date"])
        is_open_by_date = {
            row.cal_date: int(row.is_open) == 1 for row in df.itertuples()
        }
        self._open_set = frozenset(d for d in all_dates if is_open_by_date[d])

        # 前一交易日:优先用 Tushare 的 pretrade_date,缺失则正向扫描
        prev_map: dict[datetime.date, datetime.date] = {}
        if "pretrade_date" in df.columns:
            for row in df.itertuples():
                pretrade = getattr(row, "pretrade_date", None)
                if pd.isna(pretrade):
                    continue
                parsed = _parse_yyyymmdd(pretrade)
                if parsed is not None:
                    prev_map[row.cal_date] = parsed
        last_open: datetime.date | None = None
        for d in all_dates:
            if d not in prev_map and last_open is not None:
                prev_map[d] = last_open
            if is_open_by_date[d]:
                last_open = d
        self._prev_map = prev_map

        # 下一交易日:从右向左扫描
        next_map: dict[datetime.date, datetime.date] = {}
        next_open: datetime.date | None = None
        for d in reversed(all_dates):
            if next_open is not None:
                next_map[d] = next_open
            if is_open_by_date[d]:
                next_open = d
        self._next_map = next_map


def _parse_yyyymmdd(value) -> datetime.date | None:
    """解析 Tushare 日期字段,兼容字符串 '20260430' 和 CSV 读回的 float 20260430.0。"""
    if value is None:
        return None
    if isinstance(value, float):
        if pd.isna(value):
            return None
        value = int(value)
    if isinstance(value, int):
        s = str(value)
    else:
        s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        if len(s) == 8 and s.isdigit():
            return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        return pd.to_datetime(s).date()
    except Exception:
        return None


_CALENDAR: TradingCalendar | None = None
_CALENDAR_LOCK = Lock()


def get_calendar() -> TradingCalendar:
    global _CALENDAR
    with _CALENDAR_LOCK:
        if _CALENDAR is None:
            _CALENDAR = TradingCalendar()
        return _CALENDAR
