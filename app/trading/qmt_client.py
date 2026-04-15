"""QMT 交易客户端封装 —— 延迟导入 xtquant，保证回测功能不受影响。"""

from __future__ import annotations

import datetime
import sys
import threading
import time
from typing import Any

from app.trading.models import AccountInfo, PositionItem


class QMTClientError(RuntimeError):
    pass


_ORDER_STATUS_MAP = {
    48: "未报",
    49: "待报",
    50: "已报",
    51: "已报待撤",
    52: "部成待撤",
    53: "部撤",
    54: "已撤",
    55: "部成",
    56: "已成",
    57: "废单",
    255: "未知",
}

_ORDER_TYPE_MAP = {23: "买入", 24: "卖出"}


class _CallbackCollector:
    """动态基类；在 xtquant 导入后才真正继承 XtQuantTraderCallback。"""

    def __init__(self) -> None:
        self._log_lock = threading.Lock()
        self._logs: list[str] = []
        self._read_idx = 0
        self._owner: "QMTClient | None" = None

    def _set_owner(self, owner: "QMTClient") -> None:
        self._owner = owner

    def _append_log(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with self._log_lock:
            self._logs.append(f"[{ts}] {msg}")

    def drain_logs(self) -> list[str]:
        """返回自上次调用以来的新日志（不清空历史）。"""
        with self._log_lock:
            new_logs = self._logs[self._read_idx:]
            self._read_idx = len(self._logs)
        return new_logs

    def all_logs(self) -> list[str]:
        with self._log_lock:
            return list(self._logs)

    # ── 以下回调方法会被 xtquant 主动调用 ──

    def on_disconnected(self) -> None:
        self._append_log("[连接] QMT 连接断开（底层回调）")
        if self._owner is not None:
            self._owner._connected = False

    def on_stock_order(self, order: Any) -> None:
        code = getattr(order, "stock_code", "")
        otype = _ORDER_TYPE_MAP.get(getattr(order, "order_type", 0), "未知")
        status_code = getattr(order, "order_status", 255)
        status = _ORDER_STATUS_MAP.get(status_code, f"状态{status_code}")
        price = getattr(order, "price", 0)
        vol = getattr(order, "order_volume", 0)
        traded_vol = getattr(order, "traded_volume", 0)
        traded_price = getattr(order, "traded_price", 0)
        status_msg = getattr(order, "status_msg", "")
        remark = getattr(order, "order_remark", "")

        msg = f"[委托] {code} {otype} {vol}股 委托价={price}"
        msg += f"  状态={status}"
        if traded_vol:
            msg += f"  已成={traded_vol}股@{traded_price}"
        if status_msg:
            msg += f"  {status_msg}"
        if remark:
            msg += f"  ({remark})"
        self._append_log(msg)

    def on_stock_trade(self, trade: Any) -> None:
        code = getattr(trade, "stock_code", "")
        otype = _ORDER_TYPE_MAP.get(getattr(trade, "order_type", 0), "未知")
        price = getattr(trade, "traded_price", 0)
        vol = getattr(trade, "traded_volume", 0)
        amount = getattr(trade, "traded_amount", 0)
        remark = getattr(trade, "order_remark", "")

        msg = f"[成交] {code} {otype} {vol}股 成交价={price} 金额={amount}"
        if remark:
            msg += f"  ({remark})"
        self._append_log(msg)

    def on_order_error(self, order_error: Any) -> None:
        error_id = getattr(order_error, "error_id", "")
        error_msg = getattr(order_error, "error_msg", "")
        remark = getattr(order_error, "order_remark", "")
        self._append_log(f"[失败] 委托报错  错误码={error_id}  {error_msg}  ({remark})")

    def on_cancel_error(self, cancel_error: Any) -> None:
        order_id = getattr(cancel_error, "order_id", "")
        error_msg = getattr(cancel_error, "error_msg", "")
        self._append_log(f"[撤单失败] 委托号={order_id}  {error_msg}")

    def on_order_stock_async_response(self, response: Any) -> None:
        remark = getattr(response, "order_remark", "")
        order_id = getattr(response, "order_id", "")
        error_msg = getattr(response, "error_msg", "")
        if error_msg:
            self._append_log(f"[异步回报] 委托号={order_id}  失败: {error_msg}  ({remark})")
        else:
            self._append_log(f"[异步回报] 委托号={order_id}  已受理  ({remark})")

    def on_cancel_order_stock_async_response(self, response: Any) -> None:
        order_id = getattr(response, "order_id", "")
        self._append_log(f"[撤单回报] 委托号={order_id}")

    def on_account_status(self, status: Any) -> None:
        account_id = getattr(status, "account_id", "")
        stat = getattr(status, "status", "")
        self._append_log(f"[账户] {account_id} 状态变更: {stat}")


class QMTClient:
    """封装 xtquant 交易接口的单例客户端。"""

    def __init__(self) -> None:
        self._xt_trader: Any = None
        self._acc: Any = None
        self._callback: _CallbackCollector | None = None
        self._xtdata: Any = None
        self._xtconstant: Any = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ────────────────── 连接 / 断开 ──────────────────

    def connect(
        self,
        xtquant_path: str,
        userdata_path: str,
        account_id: str,
        account_type: str = "STOCK",
    ) -> None:
        if self._connected:
            return

        if xtquant_path and xtquant_path not in sys.path:
            sys.path.append(xtquant_path)

        try:
            from xtquant import xtconstant as _xtconstant
            from xtquant import xtdata as _xtdata
            from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
            from xtquant.xttype import StockAccount
        except ImportError as exc:
            raise QMTClientError(
                "无法导入 xtquant，请确认已安装 QMT 终端并在 config.toml [qmt] 中正确配置 xtquant_path"
            ) from exc
        finally:
            if xtquant_path and xtquant_path in sys.path:
                sys.path.remove(xtquant_path)

        self._xtdata = _xtdata
        self._xtconstant = _xtconstant

        callback_cls = type("_DynCallback", (XtQuantTraderCallback, _CallbackCollector), {})
        self._callback = callback_cls()
        self._callback._set_owner(self)

        session_id = int(time.time())
        self._xt_trader = XtQuantTrader(userdata_path, session_id)
        self._xt_trader.register_callback(self._callback)
        self._xt_trader.start()

        rc = self._xt_trader.connect()
        if rc != 0:
            raise QMTClientError(f"QMT 连接失败 (code={rc})，请确认 QMT 终端已启动")

        time.sleep(1)

        acc_type_map = {"STOCK": "STOCK", "CREDIT": "CREDIT", "FUTURE": "FUTURE"}
        xt_acc_type = acc_type_map.get(account_type, "STOCK")

        # 尝试订阅，如果带前导零的失败，自动去掉前导零重试
        self._acc = StockAccount(account_id, xt_acc_type)
        sub_rc = self._xt_trader.subscribe(self._acc)

        if sub_rc != 0 and account_id.lstrip("0") != account_id:
            stripped_id = account_id.lstrip("0")
            self._acc = StockAccount(stripped_id, xt_acc_type)
            sub_rc = self._xt_trader.subscribe(self._acc)

        if sub_rc != 0:
            raise QMTClientError(
                f"QMT 订阅失败 (code={sub_rc})，请检查: "
                "1) QMT 终端已登录交易账户  "
                "2) config.toml 中 account_id 与 QMT 登录账户一致"
            )

        self._connected = True

    def disconnect(self) -> None:
        if self._xt_trader is not None:
            try:
                self._xt_trader.stop()
            except Exception:
                pass
        self._xt_trader = None
        self._acc = None
        self._callback = None
        self._connected = False

    def force_disconnect(self, timeout: float = 5.0) -> None:
        """强制断开：在子线程中调用 stop() 并限制超时，防止死连接上 stop() 无限阻塞。"""
        trader = self._xt_trader
        if trader is not None:
            done = threading.Event()

            def _do_stop() -> None:
                try:
                    trader.stop()
                except Exception:
                    pass
                finally:
                    done.set()

            t = threading.Thread(target=_do_stop, daemon=True)
            t.start()
            done.wait(timeout=timeout)

        self._xt_trader = None
        self._acc = None
        self._callback = None
        self._connected = False

    def check_alive(self) -> bool:
        """真实检测连接是否存活（尝试轻量查询），失败时自动将 _connected 置 False。"""
        if not self._connected or self._xt_trader is None:
            return False
        try:
            result = self._xt_trader.query_stock_asset(self._acc)
            return result is not None
        except Exception:
            self._connected = False
            if self._callback:
                self._callback._append_log("[连接] 健康检查失败，标记为已断开")
            return False

    # ────────────────── 查询 ──────────────────

    def query_account(self) -> AccountInfo:
        self._require_connected()
        info = self._xt_trader.query_stock_asset(self._acc)
        if info is None:
            return AccountInfo()
        total = float(getattr(info, "m_dTotalAsset", 0.0))
        cash = float(getattr(info, "m_dCash", 0.0))
        mv = float(getattr(info, "m_dMarketValue", 0.0))
        if total <= 0:
            total = cash + mv
        return AccountInfo(
            total_asset=total,
            available_cash=cash,
            market_value=mv,
            frozen_cash=float(getattr(info, "m_dFrozenCash", 0.0)),
        )

    def query_positions(self) -> list[PositionItem]:
        self._require_connected()
        positions = self._xt_trader.query_stock_positions(self._acc)
        if not positions:
            return []
        items: list[PositionItem] = []

        def _first_attr(obj: Any, names: list[str]) -> Any:
            for name in names:
                value = getattr(obj, name, None)
                if value is not None:
                    return value
            return None

        for p in positions:
            vol = int(getattr(p, "m_nVolume", 0) or 0)
            if vol <= 0:
                continue
            cost_price = float(getattr(p, "m_dOpenPrice", 0.0) or 0.0)
            market_value = float(getattr(p, "m_dMarketValue", 0.0) or 0.0)
            raw_available = _first_attr(
                p,
                [
                    "m_nCanUseVolume",
                    "m_nCanUseVol",
                    "m_nAvailableVolume",
                    "m_nEnableVolume",
                    "available_volume",
                    "can_use_volume",
                ],
            )
            if raw_available is None:
                available_volume = vol
            else:
                available_volume = int(raw_available or 0)
            raw_profit = getattr(p, "m_dProfit", None)
            if raw_profit is None:
                raw_profit = getattr(p, "m_dFloatProfit", None)
            profit = float(raw_profit or 0.0)
            if abs(profit) < 1e-8 and market_value > 0 and cost_price > 0:
                profit = market_value - (cost_price * vol)
            cost_basis = cost_price * vol
            profit_rate = profit / cost_basis if cost_basis > 0 else 0.0
            stock_name = str(
                getattr(p, "m_strStockName", "")
                or getattr(p, "stock_name", "")
                or ""
            ).strip()
            items.append(
                PositionItem(
                    ts_code=getattr(p, "stock_code", ""),
                    name=stock_name,
                    volume=vol,
                    available_volume=available_volume,
                    cost_price=cost_price,
                    market_value=market_value,
                    profit=profit,
                    profit_rate=profit_rate,
                )
            )
        return items

    def get_realtime_prices(self, ts_codes: list[str]) -> dict[str, float]:
        self._require_connected()
        if not ts_codes:
            return {}
        tick = self._xtdata.get_full_tick(ts_codes)
        result: dict[str, float] = {}
        for code in ts_codes:
            info = tick.get(code)
            if info and isinstance(info, dict):
                result[code] = float(info.get("lastPrice", 0.0))
            elif info:
                result[code] = float(getattr(info, "lastPrice", 0.0))
        return result

    # ────────────────── 下单 ──────────────────

    def order_stock(
        self,
        ts_code: str,
        direction: str,
        volume: int,
        price_type: str,
        price: float,
        remark: str = "",
    ) -> int:
        self._require_connected()
        xt_dir = (
            self._xtconstant.STOCK_BUY if direction == "buy" else self._xtconstant.STOCK_SELL
        )
        if price_type == "latest":
            xt_price_type = self._xtconstant.LATEST_PRICE
            xt_price = -1
        else:
            xt_price_type = self._xtconstant.FIX_PRICE
            xt_price = price

        dir_label = "买入" if direction == "buy" else "卖出"
        price_label = "最新价" if price_type == "latest" else f"限价{price}"
        if self._callback:
            self._callback._append_log(
                f"[下单] {ts_code} {dir_label} {volume}股 {price_label}  ({remark})"
            )

        seq = self._xt_trader.order_stock_async(
            self._acc,
            ts_code,
            xt_dir,
            volume,
            xt_price_type,
            xt_price,
            remark,
            ts_code,
        )
        return seq

    def order_stock_sync(
        self,
        ts_code: str,
        direction: str,
        volume: int,
        price_type: str,
        price: float,
        remark: str = "",
    ) -> int:
        """同步下单，返回 order_id（>0 表示成功）。"""
        self._require_connected()
        xt_dir = (
            self._xtconstant.STOCK_BUY if direction == "buy" else self._xtconstant.STOCK_SELL
        )
        if price_type == "latest":
            xt_price_type = self._xtconstant.LATEST_PRICE
            xt_price = -1
        else:
            xt_price_type = self._xtconstant.FIX_PRICE
            xt_price = price

        dir_label = "买入" if direction == "buy" else "卖出"
        price_label = "最新价" if price_type == "latest" else f"限价{price:.2f}"
        if self._callback:
            self._callback._append_log(
                f"[下单] {ts_code} {dir_label} {volume}股 {price_label}  ({remark})"
            )

        order_id = self._xt_trader.order_stock(
            self._acc,
            ts_code,
            xt_dir,
            volume,
            xt_price_type,
            xt_price,
            remark,
            ts_code,
        )
        return order_id

    def cancel_order(self, order_id: int) -> None:
        """撤销委托。"""
        self._require_connected()
        if self._callback:
            self._callback._append_log(f"[撤单] 委托号={order_id}")
        self._xt_trader.cancel_order_stock(self._acc, order_id)

    def get_order_detail(self, order_id: int) -> dict[str, Any] | None:
        """按 order_id 查询单条委托，未找到时返回 None。"""
        orders = self.query_orders()
        for o in orders:
            if o["order_id"] == order_id:
                return o
        return None

    def get_latest_price(self, ts_code: str) -> float | None:
        """通过 xtdata 获取单只股票最新价。"""
        if not self._connected or self._xtdata is None:
            return None
        try:
            tick = self._xtdata.get_full_tick([ts_code])
            info = tick.get(ts_code)
            if info and isinstance(info, dict):
                p = float(info.get("lastPrice", 0.0))
            elif info:
                p = float(getattr(info, "lastPrice", 0.0))
            else:
                p = 0.0
            return p if p > 0 else None
        except Exception:
            return None

    # ────────────────── 查询当日委托 ──────────────────

    def query_orders(self) -> list[dict[str, Any]]:
        """主动查询 QMT 当日全部委托记录，返回结构化列表。"""
        self._require_connected()
        orders = self._xt_trader.query_stock_orders(self._acc)
        if not orders:
            return []
        results: list[dict[str, Any]] = []
        for o in orders:
            status_code = getattr(o, "order_status", 255)
            results.append({
                "stock_code": getattr(o, "stock_code", ""),
                "order_type": _ORDER_TYPE_MAP.get(getattr(o, "order_type", 0), "未知"),
                "order_volume": getattr(o, "order_volume", 0),
                "price": getattr(o, "price", 0),
                "traded_volume": getattr(o, "traded_volume", 0),
                "traded_price": getattr(o, "traded_price", 0),
                "order_status": _ORDER_STATUS_MAP.get(status_code, f"状态{status_code}"),
                "order_status_code": status_code,
                "status_msg": getattr(o, "status_msg", ""),
                "order_remark": getattr(o, "order_remark", ""),
                "order_id": getattr(o, "order_id", 0),
                "order_sysid": getattr(o, "order_sysid", ""),
            })
        return results

    # ────────────────── 日志 ──────────────────

    def get_execution_log(self) -> list[str]:
        """返回自上次调用以来的新增日志。"""
        if self._callback is None:
            return []
        return self._callback.drain_logs()

    def get_all_logs(self) -> list[str]:
        """返回完整日志（不影响增量读取游标）。"""
        if self._callback is None:
            return []
        return self._callback.all_logs()

    # ────────────────── 内部 ──────────────────

    def _require_connected(self) -> None:
        if not self._connected or self._xt_trader is None:
            raise QMTClientError("QMT 未连接，请先调用 connect")


# ── 模块级单例 ──
_client_instance: QMTClient | None = None
_client_lock = threading.Lock()


def get_qmt_client() -> QMTClient:
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:
                _client_instance = QMTClient()
    return _client_instance
