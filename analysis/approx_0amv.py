"""Approximate 0AMV (active market cap) for the whole A-share market.

This script does not try to reverse-engineer the proprietary Compass 0AMV
formula exactly. Instead, it builds a practical approximation from public
market-wide snapshot data:

    approx_active_cap_i =
        float_market_cap_i
        * sqrt(turnover_rate_i / 100)
        * clamp(1 + pct_change_i / 100 * momentum_beta, 0.90, 1.10)

Why this approximation is useful:
1. "流通市值" represents the tradable chip base.
2. "换手率" is a direct proxy for how much of that chip base is active.
3. "涨跌幅" adds a small trend/strength adjustment so the aggregate is less
   blind to directional participation.

Output unit:
- "近似活跃市值" uses "亿元" as the aggregate unit, which is close to the
  display style seen on many domestic stock dashboards.

Data sources:
- Default: Eastmoney full-market snapshot API
- Optional: a local CSV snapshot exported from Eastmoney / TongHuaShun /
  custom scripts, as long as it contains equivalent columns

Examples:
    python analysis/approx_0amv.py
    python analysis/approx_0amv.py --top 20
    python analysis/approx_0amv.py --csv data/snapshot_2026-03-12.csv --date 2026-03-12
    python analysis/approx_0amv.py --csv data/snapshot.csv --momentum-beta 0.25
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path
from typing import Iterable

import requests


EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    )
}
EASTMONEY_PARAMS = {
    "pn": "1",
    "pz": "100",
    "po": "1",
    "np": "1",
    "fltt": "2",
    "invt": "2",
    "fid": "f3",
    "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
    "fields": "f2,f3,f6,f8,f12,f14,f21",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
}

FIELD_ALIASES = {
    "code": ["code", "代码", "证券代码"],
    "name": ["name", "名称", "证券名称"],
    "latest_price": ["latest_price", "最新价", "现价", "收盘价"],
    "pct_change": ["pct_change", "涨跌幅", "涨幅"],
    "turnover_rate": ["turnover_rate", "换手率"],
    "float_market_cap": ["float_market_cap", "流通市值"],
    "turnover_amount": ["turnover_amount", "成交额"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="近似复刻全市场 0AMV / 活跃市值")
    parser.add_argument(
        "--csv",
        type=Path,
        help="本地快照 CSV 路径。若不传，则默认抓取东财全市场实时快照。",
    )
    parser.add_argument(
        "--date",
        default="实时",
        help="仅用于输出展示的日期标签，例如 2026-03-12。",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="输出贡献度最高的前 N 只股票，默认 10。",
    )
    parser.add_argument(
        "--momentum-beta",
        type=float,
        default=0.35,
        help="涨跌幅修正系数，默认 0.35。值越大，趋势对结果影响越强。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="网络请求超时秒数，默认 15。",
    )
    return parser.parse_args()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def to_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "None", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_csv_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, f"无法识别文件编码: {path}")


def find_column(fieldnames: Iterable[str], aliases: list[str]) -> str | None:
    normalized = {name.strip(): name for name in fieldnames if name}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def normalize_snapshot_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized_rows: list[dict[str, object]] = []
    for row in rows:
        code = str(row.get("code", "")).strip()
        name = str(row.get("name", "")).strip()
        latest_price = to_float(row.get("latest_price"))
        pct_change = to_float(row.get("pct_change"))
        turnover_rate = to_float(row.get("turnover_rate"))
        float_market_cap = to_float(row.get("float_market_cap"))
        turnover_amount = to_float(row.get("turnover_amount"))

        if not code or latest_price is None or float_market_cap is None or turnover_rate is None:
            continue
        if latest_price <= 0 or float_market_cap <= 0 or turnover_rate < 0:
            continue

        normalized_rows.append(
            {
                "code": code,
                "name": name,
                "latest_price": latest_price,
                "pct_change": pct_change or 0.0,
                "turnover_rate": turnover_rate,
                "float_market_cap": float_market_cap,
                "turnover_amount": turnover_amount or 0.0,
            }
        )
    return normalized_rows


def read_snapshot_from_csv(path: Path) -> list[dict[str, object]]:
    text = load_csv_text(path)
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError(f"CSV 缺少表头: {path}")

    field_map: dict[str, str | None] = {}
    for canonical_name, aliases in FIELD_ALIASES.items():
        field_map[canonical_name] = find_column(reader.fieldnames, aliases)

    required = ["code", "name", "latest_price", "pct_change", "turnover_rate", "float_market_cap"]
    missing = [name for name in required if not field_map.get(name)]
    if missing:
        raise ValueError(
            "CSV 缺少必要列: "
            + ", ".join(missing)
            + "。至少需要代码/名称/最新价/涨跌幅/换手率/流通市值。"
        )

    rows: list[dict[str, object]] = []
    for raw_row in reader:
        rows.append(
            {
                "code": raw_row[field_map["code"]],  # type: ignore[index]
                "name": raw_row[field_map["name"]],  # type: ignore[index]
                "latest_price": raw_row[field_map["latest_price"]],  # type: ignore[index]
                "pct_change": raw_row[field_map["pct_change"]],  # type: ignore[index]
                "turnover_rate": raw_row[field_map["turnover_rate"]],  # type: ignore[index]
                "float_market_cap": raw_row[field_map["float_market_cap"]],  # type: ignore[index]
                "turnover_amount": (
                    raw_row[field_map["turnover_amount"]] if field_map["turnover_amount"] else None
                ),
            }
        )
    return normalize_snapshot_rows(rows)


def fetch_snapshot_from_eastmoney(timeout: float) -> list[dict[str, object]]:
    all_rows: list[dict[str, object]] = []
    page = 1
    total = None
    session = requests.Session()
    session.trust_env = False

    while True:
        params = dict(EASTMONEY_PARAMS)
        params["pn"] = str(page)
        last_error: Exception | None = None
        payload = None

        for attempt in range(3):
            try:
                response = session.get(
                    EASTMONEY_URL,
                    params=params,
                    headers=EASTMONEY_HEADERS,
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(1.0 + attempt * 1.5)

        if payload is None:
            raise RuntimeError(f"东财第 {page} 页抓取失败: {last_error}")

        data = payload.get("data") or {}
        diff = data.get("diff") or []
        total = data.get("total") or total
        if not diff:
            break

        for item in diff:
            all_rows.append(
                {
                    "code": item.get("f12"),
                    "name": item.get("f14"),
                    "latest_price": item.get("f2"),
                    "pct_change": item.get("f3"),
                    "turnover_amount": item.get("f6"),
                    "turnover_rate": item.get("f8"),
                    "float_market_cap": item.get("f21"),
                }
            )

        if len(diff) < int(EASTMONEY_PARAMS["pz"]):
            break
        page += 1
        if total and len(all_rows) >= int(total):
            break

    rows = normalize_snapshot_rows(all_rows)
    if not rows:
        raise RuntimeError("东财快照返回为空，无法计算。")
    return rows


def approx_active_market_cap(
    rows: list[dict[str, object]],
    momentum_beta: float,
) -> tuple[float, list[dict[str, float | str]]]:
    total_active_cap = 0.0
    contribution_rows: list[dict[str, float | str]] = []

    for row in rows:
        turnover_rate = float(row["turnover_rate"])
        pct_change = float(row["pct_change"])
        float_market_cap_yuan = float(row["float_market_cap"])

        float_market_cap_yi = float_market_cap_yuan / 1e8
        activity_ratio = clamp(math.sqrt(turnover_rate / 100.0), 0.0, 1.0)
        momentum_factor = clamp(1.0 + pct_change / 100.0 * momentum_beta, 0.90, 1.10)
        active_cap_yi = float_market_cap_yi * activity_ratio * momentum_factor

        total_active_cap += active_cap_yi
        contribution_rows.append(
            {
                "code": str(row["code"]),
                "name": str(row["name"]),
                "float_market_cap_yi": float_market_cap_yi,
                "turnover_rate": turnover_rate,
                "pct_change": pct_change,
                "active_cap_yi": active_cap_yi,
            }
        )

    contribution_rows.sort(key=lambda item: float(item["active_cap_yi"]), reverse=True)
    return total_active_cap, contribution_rows


def print_summary(
    date_label: str,
    total_active_cap_yi: float,
    contributions: list[dict[str, float | str]],
    sample_size: int,
    top_n: int,
    momentum_beta: float,
) -> None:
    print("=" * 72)
    print(f"日期: {date_label}")
    print(f"样本股票数: {sample_size}")
    print(f"近似 0AMV / 活跃市值: {total_active_cap_yi:.2f} 亿元")
    print(f"公式参数: momentum_beta={momentum_beta}")
    print(
        "近似公式: 流通市值 × sqrt(换手率/100) × clamp(1 + 涨跌幅/100 × beta, 0.90, 1.10)"
    )
    print("=" * 72)

    if not contributions or top_n <= 0:
        return

    print("前 N 大贡献股票:")
    for index, item in enumerate(contributions[:top_n], start=1):
        print(
            f"{index:>2}. "
            f"{item['code']} {item['name']} | "
            f"贡献 {float(item['active_cap_yi']):>10.2f} 亿 | "
            f"流通市值 {float(item['float_market_cap_yi']):>10.2f} 亿 | "
            f"换手率 {float(item['turnover_rate']):>6.2f}% | "
            f"涨跌幅 {float(item['pct_change']):>6.2f}%"
        )


def main() -> int:
    args = parse_args()

    try:
        if args.csv:
            rows = read_snapshot_from_csv(args.csv)
        else:
            rows = fetch_snapshot_from_eastmoney(timeout=args.timeout)

        total_active_cap_yi, contributions = approx_active_market_cap(
            rows=rows,
            momentum_beta=args.momentum_beta,
        )
        print_summary(
            date_label=args.date,
            total_active_cap_yi=total_active_cap_yi,
            contributions=contributions,
            sample_size=len(rows),
            top_n=args.top,
            momentum_beta=args.momentum_beta,
        )
        return 0
    except requests.RequestException as exc:
        print(f"网络请求失败: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
