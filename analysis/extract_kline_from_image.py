from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
IMAGE_PATH = ROOT / "analysis" / "kline_source.png"
OUTPUT_PATH = ROOT / "analysis" / "kline_ohlc_estimated.xlsx"

# Main chart area, excluding the left axis labels and the right quote panel.
PLOT_X_START = 60
PLOT_X_END = 847

# Price gridline anchors read from the image's left axis.
GRID_ROWS = np.array([30.0, 67.0, 105.0, 142.0, 179.0])
GRID_PRICES = np.array([237520.0, 210440.0, 183360.0, 156280.0, 129100.0])
PRICE_SLOPE, PRICE_INTERCEPT = np.polyfit(GRID_ROWS, GRID_PRICES, 1)

# Values that are directly readable from the screenshot.
FIRST_DAY = {
    "date": pd.Timestamp("2025-02-28"),
    "open": 144830.6,
    "close": 140570.7,
    "high": 145183.3,
    "low": 139826.8,
    "source": "首日提示框直接读取",
}
LAST_DAY = {
    "date": pd.Timestamp("2026-03-13"),
    "open": 209690.9,
    "close": 206959.0,
    "high": 213639.0,
    "low": 206363.5,
    "source": "末日右侧报价直接读取",
}


def build_cn_trading_days() -> list[pd.Timestamp]:
    holidays = pd.to_datetime(
        [
            "2025-04-04",
            "2025-05-01",
            "2025-05-02",
            "2025-05-05",
            "2025-06-02",
            "2025-10-01",
            "2025-10-02",
            "2025-10-03",
            "2025-10-06",
            "2025-10-07",
            "2025-10-08",
            "2026-01-01",
            "2026-01-02",
            "2026-02-16",
            "2026-02-17",
            "2026-02-18",
            "2026-02-19",
            "2026-02-20",
            "2026-02-23",
        ]
    )
    all_weekdays = pd.date_range("2025-02-28", "2026-03-13", freq="B")
    return [day for day in all_weekdays if day not in holidays]


def y_to_price(y: float) -> float:
    return float(PRICE_SLOPE * y + PRICE_INTERCEPT)


def load_masks() -> tuple[np.ndarray, np.ndarray]:
    image = np.array(Image.open(IMAGE_PATH).convert("RGB"))
    plot = image[:, PLOT_X_START : PLOT_X_END + 1, :]

    red = (
        (plot[:, :, 0] > 150)
        & (plot[:, :, 1] < 145)
        & (plot[:, :, 2] < 145)
        & ((plot[:, :, 0] - plot[:, :, 1]) > 30)
    )
    green = (
        (plot[:, :, 1] > 120)
        & (plot[:, :, 0] < 175)
        & (plot[:, :, 2] < 175)
        & ((plot[:, :, 1] - plot[:, :, 0]) > 10)
    )
    return red, green


def extract_window_ohlc(
    red_mask: np.ndarray,
    green_mask: np.ndarray,
    center_x: float,
    half_width: int,
) -> dict[str, float]:
    x_center = int(round(center_x))
    x0 = max(0, x_center - half_width)
    x1 = min(red_mask.shape[1] - 1, x_center + half_width)

    red_window = red_mask[:, x0 : x1 + 1]
    green_window = green_mask[:, x0 : x1 + 1]
    color_window = red_window | green_window

    if not color_window.any():
        x0 = max(0, x_center - half_width - 2)
        x1 = min(red_mask.shape[1] - 1, x_center + half_width + 2)
        red_window = red_mask[:, x0 : x1 + 1]
        green_window = green_mask[:, x0 : x1 + 1]
        color_window = red_window | green_window

    if not color_window.any():
        return {
            "open": np.nan,
            "close": np.nan,
            "high": np.nan,
            "low": np.nan,
        }

    dominant_is_red = red_window.sum() >= green_window.sum()
    dominant_window = red_window if dominant_is_red else green_window

    all_rows = np.where(color_window.sum(axis=1) >= 1)[0]
    dom_counts = dominant_window.sum(axis=1)
    threshold = 2 if dom_counts.max() >= 2 else 1
    body_rows = np.where(dom_counts >= threshold)[0]
    if len(body_rows) == 0:
        body_rows = all_rows

    high_y = float(all_rows.min())
    low_y = float(all_rows.max())
    body_top_y = float(body_rows.min())
    body_bottom_y = float(body_rows.max())

    high = y_to_price(high_y)
    low = y_to_price(low_y)

    if dominant_is_red:
        open_price = y_to_price(body_bottom_y)
        close_price = y_to_price(body_top_y)
    else:
        open_price = y_to_price(body_top_y)
        close_price = y_to_price(body_bottom_y)

    high = max(high, open_price, close_price)
    low = min(low, open_price, close_price)

    return {
        "open": round(open_price, 1),
        "close": round(close_price, 1),
        "high": round(high, 1),
        "low": round(low, 1),
    }


def interpolate_missing(frame: pd.DataFrame) -> pd.DataFrame:
    numeric_cols = ["open", "close", "high", "low"]
    frame[numeric_cols] = frame[numeric_cols].interpolate(limit_direction="both")
    frame[numeric_cols] = frame[numeric_cols].round(1)
    return frame


def main() -> None:
    dates = build_cn_trading_days()
    red_mask, green_mask = load_masks()

    centers = np.linspace(0, red_mask.shape[1] - 1, len(dates))
    half_width = 1

    rows: list[dict[str, object]] = []
    for trade_date, center in zip(dates, centers):
        extracted = extract_window_ohlc(red_mask, green_mask, center, half_width)
        rows.append(
            {
                "date": trade_date,
                "open": extracted["open"],
                "close": extracted["close"],
                "high": extracted["high"],
                "low": extracted["low"],
                "source": "图片像素估算",
            }
        )

    frame = pd.DataFrame(rows)
    frame = interpolate_missing(frame)

    frame.loc[frame["date"] == FIRST_DAY["date"], ["open", "close", "high", "low", "source"]] = [
        FIRST_DAY["open"],
        FIRST_DAY["close"],
        FIRST_DAY["high"],
        FIRST_DAY["low"],
        FIRST_DAY["source"],
    ]
    frame.loc[frame["date"] == LAST_DAY["date"], ["open", "close", "high", "low", "source"]] = [
        LAST_DAY["open"],
        LAST_DAY["close"],
        LAST_DAY["high"],
        LAST_DAY["low"],
        LAST_DAY["source"],
    ]

    frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    frame.rename(
        columns={
            "date": "日期",
            "open": "开盘价",
            "close": "收盘价",
            "high": "最高价",
            "low": "最低价",
            "source": "数据来源",
        },
        inplace=True,
    )

    notes = pd.DataFrame(
        [
            {
                "项目": "说明",
                "内容": "本文件由单张K线截图反推生成，属于近似估算数据，不等同于交易软件原始历史数据。",
            },
            {
                "项目": "日期范围",
                "内容": "2025-02-28 至 2026-03-13，按中国A股交易日近似生成，共 252 个交易日。",
            },
            {
                "项目": "精确锚点",
                "内容": "首日 2025-02-28 和末日 2026-03-13 使用截图中可直接读出的数值覆盖写入。",
            },
            {
                "项目": "中间日期",
                "内容": "中间各日OHLC由像素颜色、蜡烛高度和价格刻度线估算得到，可能存在偏差。",
            },
        ]
    )

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="OHLC估算", index=False)
        notes.to_excel(writer, sheet_name="说明", index=False)

        sheet = writer.book["OHLC估算"]
        for column_letter, width in {
            "A": 14,
            "B": 14,
            "C": 14,
            "D": 14,
            "E": 14,
            "F": 22,
        }.items():
            sheet.column_dimensions[column_letter].width = width

        note_sheet = writer.book["说明"]
        note_sheet.column_dimensions["A"].width = 14
        note_sheet.column_dimensions["B"].width = 90

    print(f"saved: {OUTPUT_PATH}")
    print(frame.head(3).to_string(index=False))
    print(frame.tail(3).to_string(index=False))


if __name__ == "__main__":
    main()
