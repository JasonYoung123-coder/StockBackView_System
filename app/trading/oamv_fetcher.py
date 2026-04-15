"""活跃市值 (OAMV) 数据管理 & MACD 事件检测。

职责：
- 从 OAMV.XLSX 加载历史数据
- 从 https://stock.svip886.com/api/indexes 拉取当日活跃市值
- 基于 MACD(12,26,9) 计算 DIF / DEA / MACD差值
- 检测 大涨(日涨幅≥4%) / 金叉 / 死叉 事件
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_DEFAULT_XLSX = Path(__file__).resolve().parents[2] / "analysis" / "OAMV.XLSX"

OAMV_API_URL = "https://stock.svip886.com/api/indexes"

# MACD 参数
FAST_PERIOD = 12
SLOW_PERIOD = 26
SIGNAL_PERIOD = 9

BIG_RISE_PCT = 4.0  # 日涨幅 ≥ 4% 判定为"大涨"

# OAMV.XLSX 标准列名
_COL_DATE = "日期"
_COL_VALUE = "OAMV数值"
_COL_EMA12 = "EMA12"
_COL_EMA26 = "EMA26"
_COL_DIF = "DIF"
_COL_DEA = "DEA"
_COL_MACD = "MACD差值"
_COL_CHANGE_PCT = "涨跌幅%"

_STANDARD_COLS = [_COL_DATE, _COL_VALUE, _COL_EMA12, _COL_EMA26, _COL_DIF, _COL_DEA, _COL_MACD, _COL_CHANGE_PCT]


def _ema_step(value: float, prev_ema: float, period: int) -> float:
    """单步 EMA: 2/(N+1) * value + (N-1)/(N+1) * prev_ema"""
    m = 2.0 / (period + 1)
    return value * m + prev_ema * (1 - m)


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """将 XLSX 原始列名统一映射到标准列名。"""
    rename_map: dict[str, str] = {}
    for c in df.columns:
        cl = c.strip().lower()
        if cl in ("日期", "date"):
            rename_map[c] = _COL_DATE
        elif "oamv" in cl and "数值" in c:
            rename_map[c] = _COL_VALUE
        elif cl == "ema12":
            rename_map[c] = _COL_EMA12
        elif cl == "ema26":
            rename_map[c] = _COL_EMA26
        elif cl == "dif":
            rename_map[c] = _COL_DIF
        elif cl == "dea":
            rename_map[c] = _COL_DEA
        elif "macd" in cl and "差值" in c:
            rename_map[c] = _COL_MACD
    return df.rename(columns=rename_map)


class OAMVFetcher:
    """活跃市值数据管理器。

    两种使用场景：
    1. 调度器 15:01 调用 ``fetch_and_update_today()`` 拉取当日数据并追加到 XLSX。
    2. 策略实盘模式调用 ``detect_events()`` 获取历史事件列表以构建 regime 序列。
    """

    def __init__(self, xlsx_path: str | Path | None = None) -> None:
        self._path = Path(xlsx_path) if xlsx_path else _DEFAULT_XLSX
        self._df: pd.DataFrame | None = None

    # ────────────── 数据 IO ──────────────

    def load(self) -> None:
        """加载本地 OAMV.XLSX。"""
        if not self._path.exists():
            raise FileNotFoundError(f"OAMV 数据文件不存在: {self._path}")
        df = pd.read_excel(self._path)
        df = _normalize_columns(df)
        df[_COL_DATE] = pd.to_datetime(df[_COL_DATE], errors="coerce")
        df = df.dropna(subset=[_COL_DATE]).sort_values(_COL_DATE).reset_index(drop=True)
        for col in _STANDARD_COLS:
            if col not in df.columns:
                df[col] = float("nan")
        self._df = df

    def save(self, max_retries: int = 3, retry_interval: float = 2.0) -> None:
        """写回 XLSX（保留标准列名顺序）。

        若文件被占用（如 Excel 打开），会先写入临时文件再重命名；
        重命名失败时最多重试 *max_retries* 次。
        """
        if self._df is None:
            return
        out = self._df[[c for c in _STANDARD_COLS if c in self._df.columns]].copy()

        tmp_path = self._path.with_suffix(".tmp.xlsx")
        out.to_excel(tmp_path, index=False)

        for attempt in range(max_retries):
            try:
                tmp_path.replace(self._path)
                logger.info("OAMV 数据已保存至 %s (%d 行)", self._path, len(out))
                return
            except PermissionError:
                if attempt < max_retries - 1:
                    logger.warning(
                        "OAMV.XLSX 被占用（第 %d 次重试），%.0f 秒后重试...",
                        attempt + 1, retry_interval,
                    )
                    time.sleep(retry_interval)
                else:
                    logger.error(
                        "OAMV.XLSX 被占用，无法覆盖。数据已保存至临时文件: %s "
                        "请关闭正在使用该文件的程序（如 Excel），再重试。",
                        tmp_path,
                    )
                    raise PermissionError(
                        f"OAMV.XLSX 被其他程序占用，无法写入。"
                        f"请关闭 Excel 等占用该文件的程序后重试。"
                        f"（数据已暂存至 {tmp_path}）"
                    )

    # ────────────── API 拉取 ──────────────

    @staticmethod
    def fetch_today_oamv() -> tuple[float, float]:
        """从 API 获取当日活跃市值数值和涨跌幅(%)。

        Returns
        -------
        (oamv_value, change_pct)
            如 (200231.3, -2.87)
        """
        resp = requests.get(OAMV_API_URL, timeout=15)
        resp.raise_for_status()
        text = resp.text

        for line in text.splitlines():
            if "活跃市值" in line or "0AMV" in line or "OAMV" in line:
                m = re.search(r"[：:]\s*([\d.]+).*?([+-]?\d+\.?\d*)%", line)
                if m:
                    return float(m.group(1)), float(m.group(2))

        raise ValueError(f"无法从 API 响应中解析活跃市值: {text[:200]}")

    # ────────────── MACD 计算 ──────────────

    def _compute_row_macd(self, oamv_value: float, prev: pd.Series) -> dict:
        """基于前一行数据计算新行的 EMA12 / EMA26 / DIF / DEA / MACD差值。"""
        ema12 = _ema_step(oamv_value, prev[_COL_EMA12], FAST_PERIOD)
        ema26 = _ema_step(oamv_value, prev[_COL_EMA26], SLOW_PERIOD)
        dif = ema12 - ema26
        dea = _ema_step(dif, prev[_COL_DEA], SIGNAL_PERIOD)
        return {
            _COL_EMA12: ema12,
            _COL_EMA26: ema26,
            _COL_DIF: dif,
            _COL_DEA: dea,
            _COL_MACD: dif - dea,
        }

    def _recalculate_last_row(self) -> None:
        """用前一行的 EMA 状态重算最后一行的 MACD 指标。"""
        df = self._df
        if df is None or len(df) < 2:
            return
        last_idx = len(df) - 1
        prev = df.iloc[last_idx - 1]
        if any(pd.isna(prev.get(c)) for c in (_COL_EMA12, _COL_EMA26, _COL_DEA)):
            return
        vals = self._compute_row_macd(df.at[last_idx, _COL_VALUE], prev)
        for k, v in vals.items():
            df.at[last_idx, k] = v

    # ────────────── 每日更新入口 ──────────────

    def fetch_and_update_today(self) -> str | None:
        """拉取当日活跃市值 → 追加到 DataFrame → 计算 MACD → 保存 XLSX。

        Returns
        -------
        检测到的事件字符串（``"大涨"`` / ``"金叉"`` / ``"死叉"``），无事件返回 None。
        """
        if self._df is None:
            self.load()

        oamv_value, change_pct = self.fetch_today_oamv()
        today = pd.Timestamp.now().normalize()

        df = self._df
        today_mask = df[_COL_DATE] == today
        if today_mask.any():
            idx = df.index[today_mask][0]
            df.at[idx, _COL_VALUE] = oamv_value
            df.at[idx, _COL_CHANGE_PCT] = change_pct
        else:
            new_row = {c: float("nan") for c in _STANDARD_COLS}
            new_row[_COL_DATE] = today
            new_row[_COL_VALUE] = oamv_value
            new_row[_COL_CHANGE_PCT] = change_pct
            self._df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

        self._recalculate_last_row()
        self.save()

        event = self._detect_event_for_row(len(self._df) - 1, api_change_pct=change_pct)
        return event

    # ────────────── 事件检测 ──────────────

    def _detect_event_for_row(
        self,
        idx: int,
        api_change_pct: float | None = None,
    ) -> str | None:
        """检测单行事件。

        优先级：金叉/死叉 > 大涨。若同日既满足金叉又满足大涨，以金叉为准。

        Parameters
        ----------
        idx : 行索引
        api_change_pct : 由 API 直接提供的涨跌幅(%)；None 时从前后行 OAMV 计算。
        """
        df = self._df
        if df is None or idx < 1:
            return None

        # ── 金叉 / 死叉判定（优先级最高） ──
        curr_dif = df.at[idx, _COL_DIF]
        curr_dea = df.at[idx, _COL_DEA]
        prev_dif = df.at[idx - 1, _COL_DIF]
        prev_dea = df.at[idx - 1, _COL_DEA]
        if not any(pd.isna(v) for v in (curr_dif, curr_dea, prev_dif, prev_dea)):
            prev_diff = prev_dif - prev_dea
            curr_diff = curr_dif - curr_dea
            if prev_diff <= 0 < curr_diff:
                return "金叉"
            if prev_diff >= 0 > curr_diff:
                return "死叉"

        # ── 大涨判定 ──
        if api_change_pct is not None:
            pct = api_change_pct
        elif _COL_CHANGE_PCT in df.columns and not pd.isna(df.at[idx, _COL_CHANGE_PCT]):
            pct = float(df.at[idx, _COL_CHANGE_PCT])
        else:
            prev_val = df.at[idx - 1, _COL_VALUE]
            curr_val = df.at[idx, _COL_VALUE]
            if pd.isna(prev_val) or pd.isna(curr_val) or prev_val <= 0:
                pct = 0.0
            else:
                pct = (curr_val - prev_val) / prev_val * 100.0

        if pct >= BIG_RISE_PCT:
            return "大涨"

        return None

    def detect_events(self) -> list[dict]:
        """遍历所有行检测 大涨 / 金叉 / 死叉 事件。

        Returns
        -------
        list of {"date": pd.Timestamp, "status": str}
        """
        if self._df is None:
            self.load()

        events: list[dict] = []
        for i in range(1, len(self._df)):
            status = self._detect_event_for_row(i)
            if status:
                events.append({
                    "date": pd.Timestamp(self._df.at[i, _COL_DATE]).normalize(),
                    "status": status,
                })
        return events
