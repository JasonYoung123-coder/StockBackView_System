"""回测交易分析脚本

从已导出的回测结果 JSON 中筛选：
  1. 最终收益率 > 30% 的交易
  2. 最终收益率 < -12% 的交易
  3. 持仓期间峰值收益 - 最终收益 > 20% 的交易（大回撤）

为每笔筛选出的交易生成买入前后各 20 个交易日的 K 线 + 成交量图（PNG）。

用法:
    cd 项目根目录
    python -m analysis.backtest_trade_analyzer
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

# ── 确保项目根目录在 sys.path 上，以便 import app.* ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.services.tushare_client import TushareClient

# ═══════════════════ 可调参数 ═══════════════════

MIN_PROFIT_THRESHOLD = 0.30       # 收益率 > 30%
MAX_LOSS_THRESHOLD = -0.12        # 收益率 < -12%
MAX_DRAWDOWN_THRESHOLD = 0.20     # 峰值回撤 > 20%（简化差值口径）
KLINE_DAYS_BEFORE = 20            # 买入前 N 个交易日
KLINE_DAYS_AFTER = 20             # 买入后 N 个交易日（含买入日当天）

# ═══════════════════════════════════════════════

BACKTEST_CACHE_DIR = _PROJECT_ROOT / "data" / "cache" / "backtest_cache"
OUTPUT_DIR = _PROJECT_ROOT / "analysis" / "output"


# ────────────────── 步骤 1：选择回测文件 ──────────────────


def list_backtest_files() -> list[Path]:
    if not BACKTEST_CACHE_DIR.exists():
        return []
    return sorted(BACKTEST_CACHE_DIR.glob("backtest_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def choose_backtest_file() -> Path:
    files = list_backtest_files()
    if not files:
        print(f"错误: {BACKTEST_CACHE_DIR} 目录下无回测 JSON 文件")
        print("请先在前端运行回测并点击「导出本次结果 JSON」")
        sys.exit(1)

    print("可用的回测结果文件（按修改时间倒序）:\n")
    for i, fp in enumerate(files):
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = data.get("result", {})
        strategy_name = result.get("strategy", {}).get("name", "未知策略")
        start = result.get("start_date", "?")
        end = result.get("end_date", "?")
        n_trades = len(result.get("trade_records", []))
        exported = data.get("exported_at", "")[:19]
        print(f"  [{i}] {strategy_name}  {start} ~ {end}  "
              f"交易数={n_trades}  导出于 {exported}")
        print(f"      {fp.name}")

    print()
    while True:
        choice = input(f"请输入编号 [0-{len(files)-1}]（直接回车选 0）: ").strip()
        if choice == "":
            choice = "0"
        if choice.isdigit() and 0 <= int(choice) < len(files):
            return files[int(choice)]
        print("输入无效，请重新输入")


# ────────────────── 步骤 2：筛选交易 ──────────────────


def build_peak_return_map(daily_details: list[dict]) -> dict[tuple[str, str], float]:
    """遍历 daily_position_details，追踪每笔交易的持仓期间峰值 total_return。

    返回 {(ts_code, buy_date): peak_total_return}
    """
    peak: dict[tuple[str, str], float] = {}
    for day in daily_details:
        for h in day.get("holdings", []):
            key = (h["ts_code"], h["buy_date"])
            tr = h.get("total_return", 0.0)
            if key not in peak or tr > peak[key]:
                peak[key] = tr
    return peak


def filter_trades(result: dict) -> list[dict]:
    """从回测结果中筛选符合条件的交易，返回增强后的交易记录列表。"""
    trade_records = result.get("trade_records", [])
    daily_details = result.get("daily_position_details", [])

    print(f"正在从 {len(daily_details)} 天的持仓快照中计算每笔交易峰值收益...")
    peak_map = build_peak_return_map(daily_details)

    selected: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    for trade in trade_records:
        if trade.get("trade_type") == "补仓":
            continue

        ts_code = trade["ts_code"]
        buy_date = trade["buy_date"]
        ret = trade["return_rate"]
        key = (ts_code, buy_date)

        peak_ret = peak_map.get(key, ret)
        drawdown = peak_ret - ret

        reasons: list[str] = []
        if ret > MIN_PROFIT_THRESHOLD:
            reasons.append(f"收益>{MIN_PROFIT_THRESHOLD:.0%}")
        if ret < MAX_LOSS_THRESHOLD:
            reasons.append(f"亏损<{MAX_LOSS_THRESHOLD:.0%}")
        if drawdown > MAX_DRAWDOWN_THRESHOLD:
            reasons.append(f"回撤>{MAX_DRAWDOWN_THRESHOLD:.0%}")

        if reasons and key not in seen_keys:
            seen_keys.add(key)
            selected.append({
                **trade,
                "peak_return": peak_ret,
                "drawdown_from_peak": drawdown,
                "filter_reasons": ", ".join(reasons),
            })

    return selected


# ────────────────── 步骤 3：拉取 K 线数据 ──────────────────


def get_trade_dates_around(
    tushare: TushareClient, buy_date: str, before: int, after: int,
) -> tuple[str, str]:
    """获取 buy_date 前后各 N 个交易日对应的日期范围。

    返回 (start_date, end_date) 字符串 "YYYYMMDD" 格式。
    """
    cal = tushare.get_trade_calendar("20100101", "20270101")
    open_dates = sorted(cal.loc[cal["is_open"] == 1, "cal_date"].unique())

    buy_ts = pd.Timestamp(buy_date)
    # 找到 buy_date 在交易日历中的位置
    idx = None
    for i, d in enumerate(open_dates):
        if pd.Timestamp(d) >= buy_ts:
            idx = i
            break
    if idx is None:
        idx = len(open_dates) - 1

    start_idx = max(0, idx - before)
    end_idx = min(len(open_dates) - 1, idx + after)

    start_str = pd.Timestamp(open_dates[start_idx]).strftime("%Y%m%d")
    end_str = pd.Timestamp(open_dates[end_idx]).strftime("%Y%m%d")
    return start_str, end_str


def fetch_kline(
    tushare: TushareClient, ts_code: str, start_date: str, end_date: str,
) -> pd.DataFrame:
    """拉取指定股票的日线数据，返回适合 mplfinance 的 DataFrame。"""
    df = tushare.get_stock_daily(ts_code, start_date, end_date)
    if df.empty:
        return df

    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)

    # mplfinance 要求 index 为 DatetimeIndex，列名为 Open/High/Low/Close/Volume
    df = df.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "vol": "Volume",
    })
    df = df.set_index("trade_date")
    df.index.name = "Date"

    # 确保数据类型
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


# ────────────────── 步骤 4：生成 K 线图 ──────────────────


def _setup_chinese_font():
    """配置 matplotlib 中文字体，返回可用的字体路径（供 mplfinance 使用）。"""
    import matplotlib
    import matplotlib.font_manager as fm

    font_candidates = ["Microsoft YaHei", "SimHei"]
    matplotlib.rcParams["font.sans-serif"] = font_candidates + ["DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

    # 查找实际可用的字体文件路径，供 mplfinance style 使用
    for name in font_candidates:
        matches = fm.findfont(fm.FontProperties(family=name), fallback_to_default=False)
        if matches and "DejaVu" not in matches:
            return matches
    return None


# 模块级缓存，避免重复查找
_FONT_PATH: str | None = None


def generate_kline_chart(
    df: pd.DataFrame,
    trade: dict,
    output_path: Path,
) -> bool:
    """生成 K 线 + 成交量图，标注买入日和卖出日。返回是否成功。"""
    import mplfinance as mpf

    if df.empty or len(df) < 3:
        return False

    ts_code = trade["ts_code"]
    name = trade.get("name", "")
    ret = trade["return_rate"]
    peak_ret = trade.get("peak_return", ret)
    drawdown = trade.get("drawdown_from_peak", 0)
    buy_date = trade["buy_date"]
    sell_date = trade.get("sell_date", "")
    sell_reason = trade.get("sell_reason", "")
    reasons = trade.get("filter_reasons", "")

    # 标题
    title = (
        f"{ts_code} {name}   "
        f"收益: {ret:.1%}  峰值: {peak_ret:.1%}  回撤: {drawdown:.1%}\n"
        f"买入: {buy_date}  卖出: {sell_date}  原因: {sell_reason}\n"
        f"筛选条件: {reasons}"
    )

    # 标注线：买入日 / 卖出日
    vline_dates = []
    vline_colors = []
    buy_ts = pd.Timestamp(buy_date)
    if buy_ts in df.index:
        vline_dates.append(buy_ts)
        vline_colors.append("blue")
    sell_ts = pd.Timestamp(sell_date) if sell_date and sell_date != "-" else None
    if sell_ts is not None and sell_ts in df.index:
        vline_dates.append(sell_ts)
        vline_colors.append("red")

    vlines_kwargs = {}
    if vline_dates:
        vlines_kwargs = dict(
            vlines=dict(
                vlines=vline_dates,
                colors=vline_colors,
                linewidths=1.2,
                linestyle="--",
                alpha=0.8,
            )
        )

    # 涨跌配色方案（中国股市：红涨绿跌）
    mc = mpf.make_marketcolors(
        up="red", down="green",
        edge="inherit",
        wick="inherit",
        volume="in",
    )

    rc_font = {}
    if _FONT_PATH:
        rc_font = {"font.family": "sans-serif", "font.sans-serif": ["Microsoft YaHei", "SimHei"]}

    style = mpf.make_mpf_style(
        marketcolors=mc, gridstyle=":", gridcolor="#e0e0e0",
        rc=rc_font,
    )

    mpf.plot(
        df,
        type="candle",
        volume=True,
        style=style,
        title=title,
        figsize=(14, 8),
        savefig=dict(fname=str(output_path), dpi=120, bbox_inches="tight"),
        **vlines_kwargs,
    )
    return True


# ────────────────── 步骤 5：汇总报告 ──────────────────


def save_summary_csv(trades: list[dict], output_dir: Path) -> Path:
    """将筛选结果保存为 CSV 汇总报告。"""
    rows = []
    for t in trades:
        rows.append({
            "代码": t["ts_code"],
            "名称": t.get("name", ""),
            "买入日期": t["buy_date"],
            "卖出日期": t.get("sell_date", ""),
            "买入价": t["buy_price"],
            "卖出价": t["sell_price"],
            "收益率": f"{t['return_rate']:.2%}",
            "峰值收益": f"{t.get('peak_return', 0):.2%}",
            "峰值回撤": f"{t.get('drawdown_from_peak', 0):.2%}",
            "持仓天数": t.get("holding_days", 0),
            "卖出原因": t.get("sell_reason", ""),
            "筛选条件": t.get("filter_reasons", ""),
        })

    df = pd.DataFrame(rows)
    csv_path = output_dir / "筛选交易汇总.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return csv_path


# ────────────────── 主流程 ──────────────────


def main():
    print("=" * 60)
    print("  回测交易分析工具")
    print(f"  筛选条件: 收益>{MIN_PROFIT_THRESHOLD:.0%}  "
          f"亏损<{MAX_LOSS_THRESHOLD:.0%}  "
          f"回撤>{MAX_DRAWDOWN_THRESHOLD:.0%}")
    print("=" * 60)
    print()

    # 1. 选择回测文件
    backtest_file = choose_backtest_file()
    print(f"\n已选择: {backtest_file.name}")

    with open(backtest_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    result = data.get("result", {})

    strategy_name = result.get("strategy", {}).get("name", "未知策略")
    start_date = result.get("start_date", "?")
    end_date = result.get("end_date", "?")
    print(f"策略: {strategy_name}  区间: {start_date} ~ {end_date}\n")

    # 2. 筛选交易
    selected = filter_trades(result)
    if not selected:
        print("\n未找到符合筛选条件的交易，退出。")
        return

    print(f"\n筛选出 {len(selected)} 笔交易:")
    for t in selected:
        print(f"  {t['ts_code']} {t.get('name','')}  "
              f"收益={t['return_rate']:.1%}  "
              f"峰值={t.get('peak_return',0):.1%}  "
              f"回撤={t.get('drawdown_from_peak',0):.1%}  "
              f"[{t.get('filter_reasons','')}]")

    # 3. 准备输出目录
    # 以策略名+日期范围命名子目录，避免不同回测结果混淆
    safe_name = strategy_name.replace(" ", "_")
    sub_dir = f"{safe_name}_{start_date}_{end_date}"
    output_dir = OUTPUT_DIR / sub_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # 4. 配置中文字体
    global _FONT_PATH
    _FONT_PATH = _setup_chinese_font()
    if _FONT_PATH:
        print(f"中文字体: {Path(_FONT_PATH).name}")
    else:
        print("警告: 未找到中文字体，图表标题可能显示为方块")

    # 5. 逐笔拉取 K 线并生成图表
    tushare = TushareClient()
    success_count = 0
    total = len(selected)

    print(f"\n开始生成 K 线图（共 {total} 笔）...\n")
    for i, trade in enumerate(selected, 1):
        ts_code = trade["ts_code"]
        buy_date = trade["buy_date"]
        name = trade.get("name", "")

        print(f"[{i}/{total}] {ts_code} {name}  买入={buy_date}  ...", end=" ")

        try:
            start_str, end_str = get_trade_dates_around(
                tushare, buy_date, KLINE_DAYS_BEFORE, KLINE_DAYS_AFTER,
            )
            df = fetch_kline(tushare, ts_code, start_str, end_str)
            if df.empty:
                print("无K线数据，跳过")
                continue

            # 文件名：代码_买入日期_收益率
            ret_pct = f"{trade['return_rate']:+.0%}".replace("+", "p").replace("-", "n").replace("%", "")
            filename = f"{ts_code}_{buy_date}_{ret_pct}.png"
            output_path = output_dir / filename

            ok = generate_kline_chart(df, trade, output_path)
            if ok:
                success_count += 1
                print(f"OK → {filename}")
            else:
                print("数据不足，跳过")
        except Exception as e:
            print(f"错误: {e}")

    # 6. 生成汇总 CSV
    csv_path = save_summary_csv(selected, output_dir)

    print(f"\n{'=' * 60}")
    print(f"完成! 成功生成 {success_count}/{total} 张 K 线图")
    print(f"输出目录: {output_dir}")
    print(f"汇总报告: {csv_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
