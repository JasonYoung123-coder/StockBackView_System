"""策略目标权重 → 实盘股票订单转换引擎。"""

from __future__ import annotations

from app.trading.models import AccountInfo, OrderItem, PositionItem


def _round_to_lot(shares: float, ts_code: str) -> int:
    """按交易所手数规则取整。

    - 科创板 688xxx.SH : 最低 200 股，超出部分 1 股递增
    - 北交所 8xxxxx / 4xxxxx : 最低 100 股，超出部分 1 股递增
    - 其他（沪深主板、创业板）: 100 股整数倍
    """
    code_prefix = ts_code.split(".")[0]

    if code_prefix.startswith("688"):
        # 科创板: 200 股起步，超出 200 后 1 股递增
        truncated = int(shares)
        return truncated if truncated >= 200 else 0

    if code_prefix.startswith(("8", "4")) and len(code_prefix) == 6:
        # 北交所: 100 股起步，超出 100 后 1 股递增
        truncated = int(shares)
        return truncated if truncated >= 100 else 0

    # 沪深主板 / 创业板: 100 股整数倍
    return int(shares // 100) * 100


def generate_orders(
    target_weights: dict[str, float],
    positions: list[PositionItem],
    account: AccountInfo,
    prices: dict[str, float],
    price_type: str = "latest",
    name_map: dict[str, str] | None = None,
) -> list[OrderItem]:
    """根据目标权重和当前持仓生成买卖订单列表。

    返回列表中卖出订单在前、买入订单在后，以便先卖出释放资金。
    """
    name_map = name_map or {}
    total_asset = account.total_asset if account.total_asset > 0 else account.available_cash
    if total_asset <= 0:
        return []

    pos_map: dict[str, PositionItem] = {p.ts_code: p for p in positions}
    price_label = "最新价" if price_type == "latest" else "限价"

    sell_orders: list[OrderItem] = []
    buy_candidates: list[tuple[str, float]] = []

    # ── 第一轮：处理卖出 ──
    all_codes = set(target_weights.keys()) | set(pos_map.keys())
    for ts_code in all_codes:
        target_w = target_weights.get(ts_code, 0.0)
        pos = pos_map.get(ts_code)
        cur_price = prices.get(ts_code, 0.0)
        if cur_price <= 0:
            continue

        current_value = pos.available_volume * cur_price if pos else 0.0
        target_value = total_asset * target_w

        if target_value < current_value and pos and pos.available_volume > 0:
            sell_value = current_value - target_value
            sell_vol = _round_to_lot(sell_value / cur_price, ts_code)
            sell_vol = min(sell_vol, pos.available_volume)
            if sell_vol > 0:
                sell_orders.append(
                    OrderItem(
                        ts_code=ts_code,
                        name=name_map.get(ts_code, pos.name if pos else ""),
                        direction="sell",
                        price=round(cur_price, 3),
                        volume=sell_vol,
                        amount=round(sell_vol * cur_price, 2),
                        price_type=price_label,
                    )
                )

    # ── 第二轮：处理买入 ──
    estimated_sell_proceeds = sum(o.amount for o in sell_orders)
    available_cash = account.available_cash + estimated_sell_proceeds

    for ts_code in sorted(target_weights, key=lambda k: -target_weights[k]):
        target_w = target_weights[ts_code]
        if target_w <= 0:
            continue
        cur_price = prices.get(ts_code, 0.0)
        if cur_price <= 0:
            continue

        pos = pos_map.get(ts_code)
        current_value = (pos.volume * cur_price) if pos else 0.0
        target_value = total_asset * target_w

        if target_value > current_value:
            delta_value = target_value - current_value
            buy_value = min(delta_value, available_cash)
            if buy_value <= 0:
                continue
            buy_vol = _round_to_lot(buy_value / cur_price, ts_code)
            if buy_vol <= 0:
                continue
            buy_amount = buy_vol * cur_price
            if buy_amount > available_cash:
                buy_vol = _round_to_lot(available_cash / cur_price, ts_code)
                buy_amount = buy_vol * cur_price
            if buy_vol <= 0:
                continue
            buy_orders_item = OrderItem(
                ts_code=ts_code,
                name=name_map.get(ts_code, pos.name if pos else ""),
                direction="buy",
                price=round(cur_price, 3),
                volume=buy_vol,
                amount=round(buy_amount, 2),
                price_type=price_label,
            )
            sell_orders.append(buy_orders_item)
            available_cash -= buy_amount

    # sell_orders 中前半段是真正的卖出订单，后半段追加了买入订单
    # 重新整理：卖出在前、买入在后
    sells = [o for o in sell_orders if o.direction == "sell"]
    buys = [o for o in sell_orders if o.direction == "buy"]
    return sells + buys
