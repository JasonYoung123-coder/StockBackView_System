"""实盘交易调度引擎 —— 持续运行，按交易日时间自动执行策略。"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import get_settings
from app.services.market_data_service import MarketDataService
from app.services.tushare_client import TushareClient
from app.strategy.loader import StrategyLoader
from app.trading.models import AccountInfo, OrderItem, PositionItem
from app.trading.qmt_client import get_qmt_client
from app.trading.realtime_data import get_realtime_prices

logger = logging.getLogger(__name__)

SELL_EXECUTION_TIME = (14, 53)
SELL_DEADLINE = (14, 59)
SELL_SIGNAL_TIME = (21, 10)
SELL_SIGNAL_DEADLINE = (21, 20)
BUY_SIGNAL_TIME = (21, 25)
BUY_SIGNAL_DEADLINE = (21, 35)
OAMV_FETCH_TIME = (15, 5)
OAMV_DEADLINE = (15, 15)
SELL_PHASE1_TIME = (9, 25)
SELL_PHASE2_TIME = (9, 30)
SELL_MORNING_DEADLINE = (9, 35)
BUY_PHASE1_TIME = (9, 25)
BUY_PHASE2_TIME = (9, 30)
BUY_MORNING_DEADLINE = (9, 35)
MORNING_REPORT_TIME = (9, 35)
SCHEDULER_WAKE = (9, 15)
SCHEDULER_SLEEP = (22, 59)
WEEKDAYS = {0, 1, 2, 3, 4}
ORDER_RETRY_INTERVAL = 5
SELL_MAX_RETRIES = 24
BUY_MAX_RETRIES = 60
MAX_MEMORY_LOG_LINES = 500
LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"



@dataclass
class SchedulerConfig:
    strategy_name: str = ""
    fund_ratio: float = 1.0
    buy_existing: bool = False
    allow_sell: bool = True
    price_type: str = "latest"
    lookback_days: int = 250
    live_start_date: str = ""


@dataclass
class SchedulerState:
    running: bool = False
    config: SchedulerConfig = field(default_factory=SchedulerConfig)
    last_execution: str = ""
    next_execution: str = ""
    execution_log: list[str] = field(default_factory=list)
    log_seq: int = 0
    today_orders: list[OrderItem] = field(default_factory=list)
    today_executed: bool = False
    today_oamv_fetched: bool = False
    last_oamv_fetch: str = ""
    account_info: AccountInfo | None = None
    positions: list[PositionItem] = field(default_factory=list)
    error: str | None = None
    pending_sell_signals: list[dict] = field(default_factory=list)
    sell_signal_execution_date: str = ""
    today_sell_signals_generated: bool = False
    last_sell_signal_gen: str = ""
    pending_buy_signals: list[dict] = field(default_factory=list)
    buy_execution_date: str = ""
    today_buy_signals_generated: bool = False
    last_buy_signal_gen: str = ""
    strategy_target_holdings: int = 0
    strategy_holding_codes: list = field(default_factory=list)
    today_morning_report_sent: bool = False
    today_afternoon_report_sent: bool = False


class TradingScheduler:
    """单例交易调度器。"""

    _PENDING_FILE_NAME = "pending_signals.json"

    def __init__(self) -> None:
        self._state = SchedulerState()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_reconnect_date: str = ""
        self._reconnect_success: bool = False
        self._name_cache: dict[str, str] = {}

    @property
    def state(self) -> SchedulerState:
        with self._lock:
            return self._state

    # ────────────── pending 信号持久化 ──────────────

    def _pending_file_path(self) -> Path:
        return get_settings().data_dir / self._PENDING_FILE_NAME

    def _save_pending_signals(self) -> None:
        """将 pending 信号写入本地 JSON，防止进程重启丢失。"""
        with self._lock:
            data = {
                "pending_sell_signals": list(self._state.pending_sell_signals),
                "pending_buy_signals": list(self._state.pending_buy_signals),
                "strategy_target_holdings": self._state.strategy_target_holdings,
                "strategy_holding_codes": list(self._state.strategy_holding_codes),
                "buy_execution_date": self._state.buy_execution_date,
                "sell_signal_execution_date": self._state.sell_signal_execution_date,
                "today_executed": self._state.today_executed,
                "last_execution": self._state.last_execution,
                "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        try:
            path = self._pending_file_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning("保存 pending 信号失败: %s", exc)

    def _load_pending_signals(self) -> None:
        """从本地 JSON 恢复 pending 信号和执行标记（启动时调用）。"""
        path = self._pending_file_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sells = data.get("pending_sell_signals", [])
            buys = data.get("pending_buy_signals", [])
            with self._lock:
                if sells or buys:
                    self._state.pending_sell_signals = sells
                    self._state.pending_buy_signals = buys
                    self._state.strategy_target_holdings = int(data.get("strategy_target_holdings", 0))
                    self._state.strategy_holding_codes = list(data.get("strategy_holding_codes", []))
                if data.get("buy_execution_date"):
                    self._state.buy_execution_date = data["buy_execution_date"]
                if data.get("sell_signal_execution_date"):
                    self._state.sell_signal_execution_date = data["sell_signal_execution_date"]
                if data.get("today_executed"):
                    self._state.today_executed = True
                    self._state.last_execution = data.get("last_execution", "")
            updated = data.get("updated_at", "未知")
            if sells or buys:
                self._log(f"从本地恢复 pending 信号（{updated}）: "
                           f"待卖出 {len(sells)} 只, 待买入 {len(buys)} 只")
        except Exception as exc:
            logger.warning("加载 pending 信号失败: %s", exc)

    def _clear_pending_file(self) -> None:
        """清除本地 JSON（所有 pending 信号已执行或被清空）。"""
        try:
            path = self._pending_file_path()
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.warning("清除 pending 文件失败: %s", exc)

    # ────────────── 手动信号 & 接管持仓 ──────────────

    _MANUAL_SIGNALS_FILE = "manual_signals.json"
    _ADOPTED_POSITIONS_FILE = "adopted_positions.json"

    def _consume_manual_sells(self) -> list[dict]:
        """读取 data/manual_signals.json 中的手动卖出信号，读取后仅清空 sell 部分。"""
        path = get_settings().data_dir / self._MANUAL_SIGNALS_FILE
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sell = data.get("sell", [])
            if sell:
                data["sell"] = []
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return sell
        except Exception as exc:
            logger.warning("读取手动卖出信号失败: %s", exc)
            return []

    def _consume_manual_buys(self) -> list[dict]:
        """读取 data/manual_signals.json 中的手动买入信号，读取后仅清空 buy 部分。"""
        path = get_settings().data_dir / self._MANUAL_SIGNALS_FILE
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            buy = data.get("buy", [])
            if buy:
                data["buy"] = []
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return buy
        except Exception as exc:
            logger.warning("读取手动买入信号失败: %s", exc)
            return []

    def _load_adopted_positions(self) -> dict[str, dict]:
        """读取 data/adopted_positions.json 持仓注册表（含策略买入和手动买入）。"""
        path = get_settings().data_dir / self._ADOPTED_POSITIONS_FILE
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("positions", {})
        except Exception as exc:
            logger.warning("读取接管持仓失败: %s", exc)
            return {}

    def _save_adopted_positions(self, positions: dict[str, dict]) -> None:
        """写入 data/adopted_positions.json。"""
        path = get_settings().data_dir / self._ADOPTED_POSITIONS_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            data = {
                "positions": positions,
                "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning("保存接管持仓失败: %s", exc)

    def _remove_adopted_position(self, ts_code: str) -> None:
        """从持仓注册表中移除指定股票（卖出后调用）。"""
        adopted = self._load_adopted_positions()
        if ts_code in adopted:
            del adopted[ts_code]
            self._save_adopted_positions(adopted)
            self._log(f"  已从持仓注册表中移除 {ts_code}")

    def _add_adopted_position(self, ts_code: str, name: str,
                              buy_date: str, buy_price: float,
                              source: str = "manual") -> None:
        """将买入的股票加入持仓注册表（策略买入或手动买入均记录）。"""
        adopted = self._load_adopted_positions()
        adopted[ts_code] = {
            "ts_code": ts_code,
            "name": name,
            "buy_date": buy_date,
            "buy_price": round(buy_price, 3),
            "source": source,
        }
        self._save_adopted_positions(adopted)
        self._log(f"  已加入持仓注册表: {ts_code} {name} "
                  f"买入日={buy_date} 买入价={buy_price:.2f} 来源={source}")

    # ──────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._state.execution_log.append(f"[{ts}] {msg}")
            self._state.log_seq += 1
            if len(self._state.execution_log) > MAX_MEMORY_LOG_LINES + 100:
                self._flush_old_logs()
        logger.info(msg)

    def _flush_old_logs(self) -> None:
        """将超出上限的旧日志写入文件，保留最近 MAX_MEMORY_LOG_LINES 条。"""
        overflow = self._state.execution_log[:-MAX_MEMORY_LOG_LINES]
        self._state.execution_log = self._state.execution_log[-MAX_MEMORY_LOG_LINES:]
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            today = datetime.date.today().strftime("%Y-%m-%d")
            log_file = LOG_DIR / f"scheduler_{today}.log"
            with open(log_file, "a", encoding="utf-8") as f:
                for line in overflow:
                    f.write(line + "\n")
        except Exception as exc:
            logger.warning("日志写入文件失败: %s", exc)

    def start(self, config: SchedulerConfig) -> None:
        with self._lock:
            if self._state.running:
                return
            self._state = SchedulerState(running=True, config=config)
            self._stop_event.clear()

        self._load_pending_signals()

        self._log(f"策略调度已开启: {config.strategy_name}")
        self._log(f"资金比例: {config.fund_ratio:.0%}  买入已持仓: {'是' if config.buy_existing else '否'}  "
                   f"自动卖出: {'是' if config.allow_sell else '否'}")

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._state.running = False
        self._log("策略调度已关闭")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                import traceback
                self._log(f"调度异常: {exc}\n{traceback.format_exc()}")
                with self._lock:
                    self._state.error = str(exc)
            except BaseException as exc:
                import traceback
                self._log(f"调度严重异常（BaseException）: {exc}\n{traceback.format_exc()}")
                with self._lock:
                    self._state.error = f"严重异常: {exc}"
            self._stop_event.wait(timeout=30)

        with self._lock:
            self._state.running = False

    def _tick(self) -> None:
        now = datetime.datetime.now()

        if now.weekday() not in WEEKDAYS:
            self._update_next_execution(now, skip_today=True)
            return

        hm = (now.hour, now.minute)

        if hm < SCHEDULER_WAKE or hm >= SCHEDULER_SLEEP:
            self._update_next_execution(now, skip_today=hm >= SCHEDULER_SLEEP)
            return

        today_str = now.strftime("%Y-%m-%d")

        # ── 每日重连 QMT（QMT 客户端 9:00 自动重启，旧连接会失效） ──
        need_reconnect = (
            self._last_reconnect_date != today_str
            or not self._reconnect_success
        )
        if need_reconnect:
            self._reconnect_qmt()
            self._last_reconnect_date = today_str
            if not self._reconnect_success:
                return

        # ── 中途断线检测：QMT 可能在非标准时间掉线 ──
        client = get_qmt_client()
        if not client.is_connected:
            self._log("[警告] QMT 连接已断开，标记重连...")
            self._reconnect_success = False
            return

        with self._lock:
            already_executed = self._state.today_executed and self._state.last_execution.startswith(today_str)
            oamv_fetched = self._state.today_oamv_fetched and self._state.last_oamv_fetch.startswith(today_str)
            has_pending_sells = len(self._state.pending_sell_signals) > 0
            sells_exec_done = self._state.sell_signal_execution_date == today_str
            has_pending_buys = len(self._state.pending_buy_signals) > 0
            buys_done_today = self._state.buy_execution_date == today_str
            sell_sigs_generated = (
                self._state.today_sell_signals_generated
                and self._state.last_sell_signal_gen.startswith(today_str)
            )
            buy_sigs_generated = (
                self._state.today_buy_signals_generated
                and self._state.last_buy_signal_gen.startswith(today_str)
            )

        with self._lock:
            morning_report_sent = self._state.today_morning_report_sent
            afternoon_report_sent = self._state.today_afternoon_report_sent

        # ── 早盘：先卖出（释放资金），再买入 ──
        if has_pending_sells and not sells_exec_done and SELL_PHASE1_TIME <= hm <= SELL_MORNING_DEADLINE:
            self._execute_pending_sells(now)

        if has_pending_buys and not buys_done_today and BUY_PHASE1_TIME <= hm <= BUY_MORNING_DEADLINE:
            self._execute_pending_buys(now)

        if not morning_report_sent and hm >= MORNING_REPORT_TIME:
            self._send_morning_report()
            with self._lock:
                self._state.today_morning_report_sent = True

        if not already_executed and SELL_EXECUTION_TIME <= hm <= SELL_DEADLINE:
            self._execute_sell_phase(now)

        # ── 盘后：OAMV → 卖出信号生成 → 买入信号生成 ──
        if not oamv_fetched and hm >= OAMV_FETCH_TIME:
            if hm < OAMV_DEADLINE:
                self._fetch_oamv_daily(now)
            else:
                self._log(f"OAMV 拉取窗口已关闭 ({OAMV_DEADLINE[0]:02d}:{OAMV_DEADLINE[1]:02d})，跳过本日拉取")
                with self._lock:
                    self._state.today_oamv_fetched = True
                    self._state.last_oamv_fetch = now.strftime("%Y-%m-%d %H:%M:%S")

        if not sell_sigs_generated and hm >= SELL_SIGNAL_TIME:
            if hm < SELL_SIGNAL_DEADLINE:
                self._generate_sell_signals(now)
            else:
                self._log(f"卖出信号生成窗口已关闭 ({SELL_SIGNAL_DEADLINE[0]:02d}:{SELL_SIGNAL_DEADLINE[1]:02d})，跳过")
                with self._lock:
                    self._state.today_sell_signals_generated = True
                    self._state.last_sell_signal_gen = now.strftime("%Y-%m-%d %H:%M:%S")

        if not buy_sigs_generated and hm >= BUY_SIGNAL_TIME:
            if hm < BUY_SIGNAL_DEADLINE:
                self._generate_buy_signals(now)
            else:
                self._log(f"买入信号生成窗口已关闭 ({BUY_SIGNAL_DEADLINE[0]:02d}:{BUY_SIGNAL_DEADLINE[1]:02d})，跳过")
                with self._lock:
                    self._state.today_buy_signals_generated = True
                    self._state.last_buy_signal_gen = now.strftime("%Y-%m-%d %H:%M:%S")

        self._update_next_execution(now)

    def _update_next_execution(self, now: datetime.datetime, skip_today: bool = False) -> None:
        today_str = now.strftime("%Y-%m-%d")
        hm = (now.hour, now.minute)

        with self._lock:
            has_pending_sells = len(self._state.pending_sell_signals) > 0
            sells_exec_done = self._state.sell_signal_execution_date == today_str
            has_pending_buys = len(self._state.pending_buy_signals) > 0
            buys_done = self._state.buy_execution_date == today_str
            sells_done = self._state.today_executed and self._state.last_execution.startswith(today_str)
            sell_sigs_done = (
                self._state.today_sell_signals_generated
                and self._state.last_sell_signal_gen.startswith(today_str)
            )
            oamv_done = self._state.today_oamv_fetched and self._state.last_oamv_fetch.startswith(today_str)
            buy_sigs_done = (
                self._state.today_buy_signals_generated
                and self._state.last_buy_signal_gen.startswith(today_str)
            )

        if not skip_today:
            # 早盘：先卖后买
            if has_pending_sells and not sells_exec_done and hm < SELL_PHASE1_TIME:
                with self._lock:
                    self._state.next_execution = datetime.datetime.combine(
                        now.date(), datetime.time(*SELL_PHASE1_TIME),
                    ).strftime("%Y-%m-%d %H:%M 早盘卖出")
                return
            if has_pending_buys and not buys_done and hm < BUY_PHASE1_TIME:
                with self._lock:
                    self._state.next_execution = datetime.datetime.combine(
                        now.date(), datetime.time(*BUY_PHASE1_TIME),
                    ).strftime("%Y-%m-%d %H:%M 早盘买入")
                return
            # 盘中卖出
            if not sells_done and hm < SELL_EXECUTION_TIME:
                with self._lock:
                    self._state.next_execution = datetime.datetime.combine(
                        now.date(), datetime.time(*SELL_EXECUTION_TIME),
                    ).strftime("%Y-%m-%d %H:%M 盘中卖出")
                return
            # 盘后：OAMV → 卖出信号 → 买入信号
            if not oamv_done and hm < OAMV_DEADLINE:
                with self._lock:
                    next_t = OAMV_FETCH_TIME if hm < OAMV_FETCH_TIME else (hm[0], hm[1] + 1)
                    self._state.next_execution = datetime.datetime.combine(
                        now.date(), datetime.time(*next_t),
                    ).strftime("%Y-%m-%d %H:%M OAMV拉取")
                return
            if not sell_sigs_done and hm < SELL_SIGNAL_DEADLINE:
                with self._lock:
                    next_t = SELL_SIGNAL_TIME if hm < SELL_SIGNAL_TIME else (hm[0], hm[1] + 1)
                    self._state.next_execution = datetime.datetime.combine(
                        now.date(), datetime.time(*next_t),
                    ).strftime("%Y-%m-%d %H:%M 卖出信号")
                return
            if not buy_sigs_done and hm < BUY_SIGNAL_DEADLINE:
                with self._lock:
                    next_t = BUY_SIGNAL_TIME if hm < BUY_SIGNAL_TIME else (hm[0], hm[1] + 1)
                    self._state.next_execution = datetime.datetime.combine(
                        now.date(), datetime.time(*next_t),
                    ).strftime("%Y-%m-%d %H:%M 买入信号")
                return

        target_date = now.date()
        if skip_today or hm >= BUY_SIGNAL_DEADLINE:
            target_date += datetime.timedelta(days=1)
        while target_date.weekday() not in WEEKDAYS:
            target_date += datetime.timedelta(days=1)

        if has_pending_sells and not sells_exec_done:
            next_time = datetime.time(*SELL_PHASE1_TIME)
        elif has_pending_buys and not buys_done:
            next_time = datetime.time(*BUY_PHASE1_TIME)
        else:
            next_time = datetime.time(*SELL_EXECUTION_TIME)

        next_exec = datetime.datetime.combine(target_date, next_time)
        with self._lock:
            self._state.next_execution = next_exec.strftime("%Y-%m-%d %H:%M")

    # ────────────── 每日 QMT 重连 ──────────────

    def _reconnect_qmt(self) -> None:
        """断开并重新连接 QMT（QMT 客户端每日 9:00 自动重启，旧连接会失效）。"""
        from app.core.config import get_settings as _get_settings

        client = get_qmt_client()
        settings = _get_settings()

        self._log("=" * 50)
        self._log("─── 每日 QMT 重连 ───")

        # 始终强制断开（清理残留的死连接对象），使用超时防止 stop() 阻塞
        self._log("强制断开旧 QMT 连接...")
        try:
            client.force_disconnect(timeout=8)
        except Exception as exc:
            self._log(f"强制断开异常（可忽略）: {exc}")
        time.sleep(3)

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            if self._stop_event.is_set():
                return
            self._log(f"重新连接 QMT（第 {attempt}/{max_attempts} 次）...")
            try:
                client.connect(
                    xtquant_path=settings.qmt_xtquant_path,
                    userdata_path=settings.qmt_userdata_path,
                    account_id=settings.qmt_account_id,
                    account_type=settings.qmt_account_type,
                )
                if client.check_alive():
                    self._log("QMT 重连成功（健康检查通过）")
                    self._reconnect_success = True
                    with self._lock:
                        self._state.error = None
                    return
                else:
                    self._log("连接返回成功但健康检查未通过，将重试")
                    client.force_disconnect(timeout=5)
            except Exception as exc:
                self._log(f"连接失败: {exc}")
            if attempt < max_attempts:
                wait = 10 * attempt
                self._log(f"  {wait} 秒后重试...")
                for _ in range(wait):
                    if self._stop_event.is_set():
                        return
                    time.sleep(1)

        self._reconnect_success = False
        self._log("[错误] QMT 重连全部失败，将在下一个调度周期（30秒后）重试")
        with self._lock:
            self._state.error = "QMT 每日重连失败，等待重试"

    # ────────────── 策略运行核心 ──────────────

    def _run_strategy_core(self, *, realtime_only: bool = False) -> dict | None:
        """加载策略、获取行情、运行策略、查询账户。返回上下文字典，失败返回 None。

        Parameters
        ----------
        realtime_only : bool
            True  → 卖出阶段：只拉历史日线（至昨日），今日数据完全由 rt_k 实时行情注入，
                     不调用 Tushare 日线接口获取当天数据，避免盘中数据不完整。
            False → 买入阶段：优先拉取含当日的完整日线（收盘后数据已就绪），
                     若当日日线仍未到位则 fallback 到 rt_k 实时行情注入。
        """
        config = self._state.config

        client = get_qmt_client()
        if not client.is_connected:
            self._log("[错误] QMT 未连接")
            return None

        loader = StrategyLoader()
        tushare_client = TushareClient()
        market_service = MarketDataService(tushare_client=tushare_client)

        self._log("加载策略...")
        strategy = loader.get_strategy(config.strategy_name)

        today = datetime.date.today()
        today_ts = pd.Timestamp(today)
        lookback_start = today - datetime.timedelta(days=int(config.lookback_days * 1.6))

        if realtime_only:
            # ── 卖出模式：只拉到昨日的日线，今日数据由 rt_k 提供 ──
            yesterday = today - datetime.timedelta(days=1)
            self._log(f"获取历史日线（至 {yesterday}，不含今日）...")
            market_data, trade_dates = market_service.get_market_history(
                lookback_start.strftime("%Y%m%d"),
                yesterday.strftime("%Y%m%d"),
                lookback_days=0,
            )
        else:
            # ── 买入模式：拉取含今日的完整日线 ──
            self._log("获取历史日线（含今日）...")
            market_data, trade_dates = market_service.get_market_history(
                lookback_start.strftime("%Y%m%d"),
                today.strftime("%Y%m%d"),
                lookback_days=0,
            )

        if market_data.empty or not trade_dates:
            self._log("[错误] 市场数据为空")
            return None

        existing_dates = set(pd.to_datetime(market_data["trade_date"]).dt.normalize().unique())
        need_realtime = realtime_only or (today_ts not in existing_dates)

        realtime_prices: dict = {}
        if need_realtime:
            label = "实时行情（盘中 rt_k）" if realtime_only else "实时行情（日线缺失 fallback）"
            self._log(f"通过 Tushare rt_k 获取{label}...")
            all_codes = sorted(market_data["ts_code"].unique().tolist())
            for attempt in range(3):
                realtime_prices = get_realtime_prices(all_codes)
                if realtime_prices:
                    break
                if attempt < 2:
                    self._log(f"  rt_k 第 {attempt + 1} 次获取为空，5秒后重试...")
                    time.sleep(5)
            self._log(f"获取到 {len(realtime_prices)} 只股票的实时行情")

            if realtime_prices and today_ts not in existing_dates:
                self._log("注入实时行情作为今日数据...")
                market_data, trade_dates = self._inject_realtime_data(
                    market_data, trade_dates, today_ts, realtime_prices
                )
            elif not realtime_prices:
                if today_ts in existing_dates:
                    self._log("[警告] 实时行情为空，但已有今日日线数据，继续执行")
                else:
                    self._log("[警告] 实时行情为空且无今日数据，将使用最近历史数据运行策略")
        else:
            self._log("今日日线数据已就绪，无需拉取实时行情")

        # 实盘买入模式下，策略在最后一天需要 realtime_prices 来确定买入成交价。
        # 若日线数据已就绪但 realtime_prices 为空，用今日收盘价填充，
        # 确保 generate_live_signals 中 _get_buy_execution 能正确定价。
        if not realtime_only and not realtime_prices and today_ts in existing_dates:
            today_data = market_data.loc[
                pd.to_datetime(market_data["trade_date"]).dt.normalize() == today_ts
            ]
            for _, row in today_data.iterrows():
                ts_code = str(row["ts_code"])
                close_price = float(row.get("close", 0))
                if close_price > 0:
                    realtime_prices[ts_code] = {"latest_price": close_price}
            if realtime_prices:
                self._log(f"已从今日日线数据构建 {len(realtime_prices)} 只股票的收盘价作为实盘成交参考")

        # ── 先查 QMT 持仓，用于传入策略做持仓对齐 ──
        self._log("查询 QMT 账户持仓...")
        account_info = client.query_account()
        qmt_positions = client.query_positions()
        with self._lock:
            self._state.account_info = account_info
            self._state.positions = qmt_positions

        qmt_pos_map = {p.ts_code: p for p in qmt_positions}
        held_codes = {p.ts_code for p in qmt_positions if p.volume > 0}

        total_for_strategy = account_info.total_asset * config.fund_ratio
        self._log(f"账户总资产: {account_info.total_asset:,.2f}  "
                   f"策略资金池({config.fund_ratio:.0%}): {total_for_strategy:,.2f}  "
                   f"QMT 当前持仓: {len(held_codes)} 只")

        # ── 资金变动检测 ──
        self._detect_fund_change(account_info)

        # ── 读取持仓注册表 ──
        adopted = self._load_adopted_positions()
        # 清理 QMT 已不持有的注册条目（兜底：防止崩溃等导致卖出后未正常清理）
        stale = [code for code in adopted if code not in held_codes]
        if stale:
            for code in stale:
                del adopted[code]
            self._save_adopted_positions(adopted)
            self._log(f"清理持仓注册表中已不存在的股票: {', '.join(stale)}")
        if adopted:
            self._log(f"已加载 {len(adopted)} 只持仓注册: "
                       f"{', '.join(adopted.keys())}")

        # ── 运行策略（传入 QMT 实际持仓供策略对齐） ──
        live_start = config.live_start_date or today.strftime("%Y-%m-%d")
        self._log(f"运行策略生成实盘信号（起始日: {live_start}）...")
        if hasattr(strategy.instance, "generate_live_signals"):
            signals = strategy.instance.generate_live_signals(
                market_data, trade_dates, realtime_prices,
                live_start_date=live_start,
                qmt_held_codes=held_codes,
                adopted_positions=adopted,
                skip_score_replace=realtime_only,
            )
        else:
            self._log("[警告] 策略不支持实盘信号，回退到回测模式")
            weights, meta = strategy.instance.generate_portfolio_weights(
                market_data, trade_dates
            )
            signals = {
                "sell_signals": [],
                "buy_signals": [],
                "holdings": meta.get("latest_holdings", []),
            }

        sell_signals = signals.get("sell_signals", [])
        buy_signals = signals.get("buy_signals", [])
        strategy_holdings = signals.get("holdings", [])
        strategy_warnings = signals.get("warnings", [])
        strategy_diagnostics = signals.get("diagnostics", [])
        self._log(f"策略信号: 卖出 {len(sell_signals)} 只, 买入 {len(buy_signals)} 只, "
                   f"策略目标持仓 {len(strategy_holdings)} 只")

        if strategy_warnings:
            self._log(f"策略警告 ({len(strategy_warnings)} 条):")
            for w in strategy_warnings[-10:]:
                self._log(f"  {w}")
            if len(strategy_warnings) > 10:
                self._log(f"  ...共 {len(strategy_warnings)} 条，仅显示最近 10 条")

        if strategy_diagnostics:
            self._log(f"选股诊断 ({len(strategy_diagnostics)} 条):")
            for d in strategy_diagnostics:
                self._log(f"  {d}")

        # ── 持仓对齐日志 ──
        self._log_position_reconciliation(
            strategy_holdings, held_codes, qmt_pos_map,
            sell_signals=sell_signals, buy_signals=buy_signals,
        )

        name_map: dict[str, str] = {}
        if not market_data.empty and "name" in market_data.columns:
            dedup = market_data.drop_duplicates(subset=["ts_code"], keep="last")
            name_map = dict(zip(dedup["ts_code"].astype(str), dedup["name"].astype(str)))

        self._name_cache.update(name_map)
        for p in qmt_positions:
            if not p.name and p.ts_code in self._name_cache:
                p.name = self._name_cache[p.ts_code]

        return {
            "sell_signals": sell_signals,
            "buy_signals": buy_signals,
            "holdings": strategy_holdings,
            "warnings": strategy_warnings,
            "diagnostics": strategy_diagnostics,
            "qmt_pos_map": qmt_pos_map,
            "held_codes": held_codes,
            "name_map": name_map,
        }

    # ────────────── 14:53 卖出阶段 ──────────────

    def _execute_sell_phase(self, now: datetime.datetime) -> None:
        """14:53 执行卖出信号（含撤单重新委托）。"""
        config = self._state.config
        self._log("=" * 50)
        self._log(f"─── {SELL_EXECUTION_TIME[0]:02d}:{SELL_EXECUTION_TIME[1]:02d} 卖出阶段: {config.strategy_name} ───")

        result = self._run_strategy_core(realtime_only=True)
        if result is None:
            return

        sell_signals = result["sell_signals"]
        qmt_pos_map = result["qmt_pos_map"]
        name_map = result["name_map"]
        sell_orders: list[OrderItem] = []

        if config.allow_sell and sell_signals:
            self._notify_sell_phase(sell_signals, name_map)
            client = get_qmt_client()
            self._log("执行卖出（先全部首轮委托，再统一撤单重新委托，间隔 %ds）..." % ORDER_RETRY_INTERVAL)
            active_orders: dict[str, dict[str, Any]] = {}

            for sig in sell_signals:
                ts_code = sig["ts_code"]
                pos = qmt_pos_map.get(ts_code)
                if not pos or pos.available_volume <= 0:
                    self._log(f"  {ts_code}: QMT 无可卖持仓，跳过")
                    continue
                name = name_map.get(ts_code, "")
                sell_vol = pos.available_volume
                reason = sig.get("reason", "策略卖出")
                price = client.get_latest_price(ts_code)
                price_label = f"@{price:.2f}" if price else "@最新价"

                self._log(f"  卖出 {ts_code} {name} {sell_vol}股 {price_label} ({reason})")
                order_id = client.order_stock_sync(
                    ts_code=ts_code,
                    direction="sell",
                    volume=sell_vol,
                    price_type="latest",
                    price=-1,
                    remark=f"sell_{reason}_phase1",
                )
                if order_id > 0:
                    active_orders[ts_code] = {
                        "order_id": order_id,
                        "name": name,
                        "volume": sell_vol,
                        "remaining": sell_vol,
                        "total_traded": 0,
                        "reason": reason,
                    }
                    self._log(f"    首轮委托已提交 (order_id={order_id})")
                else:
                    self._log(f"    首轮委托失败 (order_id={order_id})")

                sell_orders.append(OrderItem(
                    ts_code=ts_code, name=name, direction="sell",
                    price=0, volume=sell_vol, amount=0,
                    price_type="最新价", remark=reason,
                    status="submitted" if order_id > 0 else "failed",
                ))

            if active_orders:
                self._log("开始统一轮询卖出委托状态...")
                for attempt in range(SELL_MAX_RETRIES):
                    if self._stop_event.is_set():
                        break

                    now_hm = (datetime.datetime.now().hour, datetime.datetime.now().minute)
                    if now_hm >= SELL_DEADLINE:
                        self._log(
                            f"已过截止时间 {SELL_DEADLINE[0]:02d}:{SELL_DEADLINE[1]:02d}，停止卖出补单"
                        )
                        break

                    if attempt > 0:
                        time.sleep(ORDER_RETRY_INTERVAL)

                    still_pending: dict[str, dict[str, Any]] = {}
                    for ts_code, info in active_orders.items():
                        order_id = int(info.get("order_id", 0) or 0)
                        if order_id <= 0:
                            still_pending[ts_code] = dict(info)
                            continue

                        detail = client.get_order_detail(order_id)
                        if detail is None:
                            still_pending[ts_code] = dict(info)
                            continue

                        status_code = detail.get("order_status_code", 255)
                        traded = int(detail.get("traded_volume", 0))
                        remaining = max(int(info["remaining"]) - traded, 0)
                        total_traded = int(info.get("total_traded", 0)) + traded

                        if status_code == 56 or remaining <= 0:
                            self._log(f"  {ts_code} {info['name']} 卖出已全部成交")
                            continue

                        still_pending[ts_code] = {
                            **info,
                            "remaining": remaining,
                            "total_traded": total_traded,
                        }

                    if not still_pending:
                        self._log("所有卖出委托已成交")
                        active_orders = {}
                        break

                    next_active_orders: dict[str, dict[str, Any]] = {}
                    for ts_code, info in still_pending.items():
                        old_id = int(info.get("order_id", 0) or 0)
                        remaining = int(info["remaining"])
                        if remaining <= 0:
                            continue

                        if old_id > 0:
                            try:
                                client.cancel_order(old_id)
                            except Exception as exc:
                                self._log(f"  撤单异常 {ts_code} order_id={old_id}: {exc}")
                            time.sleep(1)
                            # 撤单后重新查询老委托的最终成交量
                            post_cancel = client.get_order_detail(old_id)
                            if post_cancel is not None:
                                final_traded = int(post_cancel.get("traded_volume", 0))
                                if final_traded > 0:
                                    newly_filled = max(final_traded - (int(info["volume"]) - remaining), 0)
                                    remaining = max(remaining - newly_filled, 0)
                                    info = {**info,
                                            "remaining": remaining,
                                            "total_traded": int(info.get("total_traded", 0)) + newly_filled}
                            self._log(f"  撤单 {ts_code} order_id={old_id}，剩余 {remaining}股")

                        if remaining <= 0:
                            self._log(f"  {ts_code} {info['name']} 卖出已全部成交（撤单后确认）")
                            continue

                        new_price = client.get_latest_price(ts_code)
                        price_label = f"@{new_price:.2f}" if new_price else "@最新价"
                        new_id = client.order_stock_sync(
                            ts_code=ts_code,
                            direction="sell",
                            volume=remaining,
                            price_type="latest",
                            price=new_price or -1,
                            remark=f"sell_{info['reason']}_retry{attempt}",
                        )
                        if new_id > 0:
                            next_active_orders[ts_code] = {
                                **info,
                                "order_id": new_id,
                            }
                            self._log(
                                f"  重新委托 {ts_code} {info['name']} 卖出 {remaining}股 "
                                f"{price_label} (order_id={new_id})"
                            )
                        else:
                            next_active_orders[ts_code] = {
                                **info,
                                "order_id": 0,
                            }
                            self._log(f"  {ts_code} 重新委托失败 (order_id={new_id})")

                    active_orders = next_active_orders

                self._collect_qmt_logs()

                final_orders: list[OrderItem] = []
                for order in sell_orders:
                    info = active_orders.get(order.ts_code)
                    if order.status == "failed" and info is None:
                        final_orders.append(order)
                        continue
                    if info is None:
                        final_orders.append(order.model_copy(update={"status": "filled"}))
                        continue

                    total_traded = int(info.get("total_traded", 0))
                    if total_traded > 0:
                        status = "partial"
                    else:
                        status = "failed"
                    final_orders.append(order.model_copy(update={"status": status}))
                sell_orders = final_orders
        elif not config.allow_sell and sell_signals:
            self._log(f"策略建议卖出 {len(sell_signals)} 只，但[自动卖出]未开启，跳过")
        else:
            self._log("无卖出信号")
            self._notify_no_sell_phase()

        with self._lock:
            self._state.today_orders = sell_orders
            self._state.today_executed = True
            self._state.last_execution = now.strftime("%Y-%m-%d %H:%M:%S")
        self._save_pending_signals()
        if sell_orders:
            self._notify_execution_result("盘中卖出", sell_orders)
        self._log(f"卖出阶段完毕，盘后卖出信号生成将于 {SELL_SIGNAL_TIME[0]:02d}:{SELL_SIGNAL_TIME[1]:02d} 执行")

    # ────────────── 21:10 盘后卖出信号生成 ──────────────

    def _generate_sell_signals(self, now: datetime.datetime) -> None:
        """21:10~21:20 用完整日线+筹码数据重跑策略，生成卖出信号。

        逻辑：
        1. 以完整日线（含当日收盘）重跑策略，获取全量卖出信号。
        2. 排除 14:53 已经成功执行的卖出（today_orders）。
        3. 剩余的即为"遗漏卖出"（含筹码止盈 + 死叉次日卖出等），
           存入 pending_sell_signals，次日 9:26 执行。
        """
        config = self._state.config
        self._log("=" * 50)
        self._log(f"─── {SELL_SIGNAL_TIME[0]:02d}:{SELL_SIGNAL_TIME[1]:02d} "
                  f"盘后卖出信号生成: {config.strategy_name} ───")

        result = self._run_strategy_core()
        if result is None:
            with self._lock:
                self._state.today_sell_signals_generated = True
                self._state.last_sell_signal_gen = now.strftime("%Y-%m-%d %H:%M:%S")
            return

        sell_signals = result["sell_signals"]
        held_codes = result["held_codes"]
        name_map = result["name_map"]

        with self._lock:
            already_sold = {o.ts_code for o in self._state.today_orders if o.direction == "sell"}

        if sell_signals:
            pending: list[dict] = []
            for sig in sell_signals:
                ts_code = sig["ts_code"]
                if ts_code in already_sold:
                    self._log(f"  {ts_code}: 盘中已卖出，跳过")
                    continue
                if ts_code not in held_codes:
                    self._log(f"  {ts_code}: QMT 无持仓，跳过")
                    continue
                reason = sig.get("reason", "策略卖出")
                pending.append({
                    "ts_code": ts_code,
                    "name": name_map.get(ts_code, ""),
                    "reason": reason,
                })
            if pending:
                with self._lock:
                    self._state.pending_sell_signals = pending
                self._save_pending_signals()
                self._log(f"发现 {len(pending)} 只遗漏卖出，将于下一交易日 9:26 委托卖出")
                for p in pending:
                    self._log(f"  待卖出: {p['ts_code']} {p['name']} ({p['reason']})")
                self._notify_sell_signals(pending)
            else:
                self._log("卖出信号扫描完成，无遗漏卖出信号")
                self._notify_no_sell_signal(len(already_sold))
        else:
            self._log("卖出信号扫描完成，策略无卖出信号")
            with self._lock:
                already_sold_count = sum(1 for o in self._state.today_orders if o.direction == "sell")
            self._notify_no_sell_signal(already_sold_count)

        # ── 合并手动卖出信号 ──
        manual_sells = self._consume_manual_sells()
        if manual_sells:
            self._log(f"检测到 {len(manual_sells)} 只手动卖出信号")
            with self._lock:
                existing_codes = {s["ts_code"] for s in self._state.pending_sell_signals}
            for ms in manual_sells:
                ts_code = ms.get("ts_code", "")
                if not ts_code:
                    continue
                if ts_code in existing_codes:
                    self._log(f"  {ts_code}: 策略已产生卖出信号，跳过手动信号")
                    continue
                if ts_code not in held_codes if result else True:
                    self._log(f"  {ts_code}: QMT 无持仓，跳过")
                    continue
                reason = ms.get("reason", "手动卖出")
                name = ms.get("name", "") or (name_map.get(ts_code, "") if result else "")
                manual_entry = {"ts_code": ts_code, "name": name,
                                "reason": reason, "manual": True}
                with self._lock:
                    self._state.pending_sell_signals.append(manual_entry)
                self._log(f"  手动待卖出: {ts_code} {name} ({reason})")
            if manual_sells:
                self._save_pending_signals()

        with self._lock:
            self._state.today_sell_signals_generated = True
            self._state.last_sell_signal_gen = now.strftime("%Y-%m-%d %H:%M:%S")
            if self._state.pending_sell_signals:
                self._state.sell_signal_execution_date = now.strftime("%Y-%m-%d")
        self._log("卖出信号生成完毕")

    # ────────────── 21:25 盘后买入信号生成 ──────────────

    def _generate_buy_signals(self, now: datetime.datetime) -> None:
        """21:25~21:35 用完整日线重跑策略生成买入信号，保存为待买入标的（T+1 日 9:26 执行）。

        安排在卖出信号生成（21:10）之后，确保：
        1. Tushare 日线数据已就绪（收盘后数据完整）
        2. 卖出信号已生成，可正确计算可用仓位（扣除待卖出腾出的仓位）
        """
        config = self._state.config
        self._log("=" * 50)
        self._log(f"─── {BUY_SIGNAL_TIME[0]:02d}:{BUY_SIGNAL_TIME[1]:02d} "
                  f"买入信号生成: {config.strategy_name} ───")

        result = self._run_strategy_core()
        if result is None:
            with self._lock:
                self._state.today_buy_signals_generated = True
                self._state.last_buy_signal_gen = now.strftime("%Y-%m-%d %H:%M:%S")
            return

        buy_signals = result["buy_signals"]
        held_codes = result["held_codes"]
        name_map = result["name_map"]
        strategy_holdings = result["holdings"]
        strategy_warnings = result.get("warnings", [])
        strategy_diagnostics = result.get("diagnostics", [])

        if strategy_diagnostics:
            self._log(f"选股诊断 ({len(strategy_diagnostics)} 条):")
            for d in strategy_diagnostics:
                self._log(f"  {d}")

        # 统一提取策略目标持仓代码集合（兼容 list[dict] 和 dict 两种格式）
        if isinstance(strategy_holdings, dict):
            strategy_codes = set(strategy_holdings.keys())
        else:
            strategy_codes = {h.get("ts_code", "") for h in strategy_holdings if h.get("ts_code")}
        strategy_target = len(strategy_codes) or len(strategy_holdings)

        if buy_signals:
            pending: list[dict] = []
            for sig in buy_signals:
                ts_code = sig["ts_code"]
                if not config.buy_existing and ts_code in held_codes:
                    self._log(f"  {ts_code}: 已持仓且[买入已持仓]未开启，跳过")
                    continue
                pending.append({
                    "ts_code": ts_code,
                    "name": name_map.get(ts_code, ""),
                })
            # ── 仓位上限校验：只计算策略范围内持仓，考虑待卖出腾出的仓位 ──
            strategy_held_count = len(held_codes & strategy_codes)
            with self._lock:
                pending_sell_count = len(self._state.pending_sell_signals)
            available_slots = max(
                0, strategy_target - strategy_held_count + pending_sell_count
            )
            if len(pending) > available_slots:
                skipped = pending[available_slots:]
                pending = pending[:available_slots]
                for p in skipped:
                    self._log(f"  {p['ts_code']} {p['name']}: "
                              f"超出可用仓位({available_slots}/{strategy_target}，"
                              f"策略内持仓{strategy_held_count}只)，跳过")

            if pending:
                with self._lock:
                    self._state.pending_buy_signals = pending
                    self._state.strategy_target_holdings = strategy_target
                    self._state.strategy_holding_codes = list(strategy_codes)
                self._save_pending_signals()
                self._log(f"已生成 {len(pending)} 只待买入标的，将于下一交易日 9:26 委托")
                for p in pending:
                    self._log(f"  待买入: {p['ts_code']} {p['name']}")
                self._notify_buy_signals(pending)
            else:
                self._log("无有效买入信号（候选标的均已持仓或仓位已满）")
                self._notify_no_buy_signal(len(held_codes), strategy_target, strategy_warnings)
        else:
            self._log("无买入信号")
            self._notify_no_buy_signal(
                len(strategy_holdings), strategy_target, strategy_warnings,
            )

        # ── 合并手动买入信号（不受仓位上限约束） ──
        manual_buys = self._consume_manual_buys()
        if manual_buys:
            self._log(f"检测到 {len(manual_buys)} 只手动买入信号")
            with self._lock:
                existing_codes = {s["ts_code"] for s in self._state.pending_buy_signals}
            for mb in manual_buys:
                ts_code = mb.get("ts_code", "")
                if not ts_code:
                    continue
                if ts_code in existing_codes:
                    self._log(f"  {ts_code}: 已在待买入列表中，跳过")
                    continue
                name = mb.get("name", "") or (name_map.get(ts_code, "") if result else "")
                manual_entry = {"ts_code": ts_code, "name": name, "manual": True}
                with self._lock:
                    self._state.pending_buy_signals.append(manual_entry)
                self._log(f"  手动待买入: {ts_code} {name}")
            if manual_buys:
                self._save_pending_signals()

        with self._lock:
            self._state.today_buy_signals_generated = True
            self._state.last_buy_signal_gen = now.strftime("%Y-%m-%d %H:%M:%S")
            if self._state.pending_buy_signals:
                self._state.buy_execution_date = now.strftime("%Y-%m-%d")
        self._log("买入信号生成完毕")

    # ────────────── T+1 早盘待卖出执行 ──────────────

    def _execute_pending_sells(self, now: datetime.datetime) -> None:
        """T+1 早盘执行待卖出：9:26 首次委托 → 等待至 9:30 → 撤单重委，与买入逻辑对称。"""
        with self._lock:
            pending = list(self._state.pending_sell_signals)
            config = self._state.config
        if not pending:
            return

        self._log("=" * 50)
        self._log(f"─── 早盘卖出阶段（{len(pending)} 只待卖出） ───")

        client = get_qmt_client()
        if not client.is_connected:
            self._log("[错误] QMT 未连接，跳过卖出执行")
            return

        qmt_positions = client.query_positions()
        pos_map = {p.ts_code: p for p in qmt_positions}

        active_orders: dict[str, dict] = {}
        sell_order_items: list[OrderItem] = []

        for sig in pending:
            ts_code = sig["ts_code"]
            name = sig.get("name", "")
            reason = sig.get("reason", "盘后卖出")
            pos = pos_map.get(ts_code)
            if not pos or pos.available_volume <= 0:
                self._log(f"  {ts_code} {name}: QMT 无可卖持仓，跳过")
                continue

            specified_vol = sig.get("volume")
            if specified_vol and int(specified_vol) > 0:
                sell_vol = min(int(specified_vol), pos.available_volume)
                if int(specified_vol) > pos.available_volume:
                    self._log(f"  {ts_code} {name}: 指定卖出 {specified_vol}股 > 可卖 {pos.available_volume}股，按可卖数量执行")
            else:
                sell_vol = pos.available_volume
            price = client.get_latest_price(ts_code)
            price_label = f"@{price:.2f}" if price else "@最新价"

            order_id = client.order_stock_sync(
                ts_code=ts_code, direction="sell", volume=sell_vol,
                price_type="latest", price=-1, remark="sell_pending_phase1",
            )
            if order_id > 0:
                active_orders[ts_code] = {
                    "order_id": order_id, "name": name,
                    "volume": sell_vol, "remaining": sell_vol,
                }
                self._log(f"  委托卖出 {ts_code} {name} {sell_vol}股 "
                           f"{price_label} ({reason}) (order_id={order_id})")
            else:
                self._log(f"  {ts_code} {name} 卖出下单失败 (order_id={order_id})")

            sell_order_items.append(OrderItem(
                ts_code=ts_code, name=name, direction="sell",
                price=round(price, 3) if price else 0, volume=sell_vol,
                amount=0, price_type="最新价", remark=f"T+1卖出-{reason}",
            ))

        with self._lock:
            self._state.today_orders.extend(sell_order_items)

        if not active_orders:
            self._log("无有效卖出委托")
            with self._lock:
                self._state.pending_sell_signals = []
                self._state.sell_signal_execution_date = now.strftime("%Y-%m-%d")
            self._save_pending_signals()
            return

        # ── 等待至 9:30 ──
        now_dt = datetime.datetime.now()
        target_930 = now_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_dt < target_930:
            wait_secs = (target_930 - now_dt).total_seconds()
            self._log(f"等待至 9:30 检查卖出委托状态（{wait_secs:.0f}秒）...")
            waited = 0.0
            while waited < wait_secs and not self._stop_event.is_set():
                step = min(5.0, wait_secs - waited)
                time.sleep(step)
                waited += step

        # ── Phase 2: 撤单重新委托轮询 ──
        self._log("─── 9:30 卖出撤单重新委托轮询 ───")
        for attempt in range(SELL_MAX_RETRIES):
            if self._stop_event.is_set():
                break

            still_pending: dict[str, dict] = {}
            for ts_code, info in active_orders.items():
                detail = client.get_order_detail(info["order_id"])
                if detail is None:
                    still_pending[ts_code] = info
                    continue
                status_code = detail.get("order_status_code", 255)
                traded = int(detail.get("traded_volume", 0))
                if status_code == 56 or traded >= info["remaining"]:
                    self._log(f"  {ts_code} {info['name']} 卖出已全部成交")
                    continue
                remaining = info["remaining"] - traded
                if remaining <= 0:
                    continue
                still_pending[ts_code] = {**info, "remaining": remaining}

            if not still_pending:
                self._log("所有卖出委托已成交")
                break

            for ts_code, info in still_pending.items():
                old_id = info["order_id"]
                remaining = info["remaining"]
                try:
                    client.cancel_order(old_id)
                except Exception as exc:
                    self._log(f"  撤单异常 {ts_code} order_id={old_id}: {exc}")
                time.sleep(1)
                # 撤单后重新查询老委托的最终成交量
                post_cancel = client.get_order_detail(old_id)
                if post_cancel is not None:
                    final_traded = int(post_cancel.get("traded_volume", 0))
                    if final_traded > 0:
                        newly_filled = max(final_traded - (int(info["volume"]) - remaining), 0)
                        remaining = max(remaining - newly_filled, 0)
                        info = {**info, "remaining": remaining}
                self._log(f"  撤单 {ts_code} order_id={old_id}，剩余 {remaining}股")

                if remaining <= 0:
                    self._log(f"  {ts_code} {info['name']} 卖出已全部成交（撤单后确认）")
                    active_orders[ts_code] = {**info, "order_id": old_id, "remaining": 0}
                    continue

                new_price = client.get_latest_price(ts_code)
                price_label = f"@{new_price:.2f}" if new_price else "@最新价"
                new_id = client.order_stock_sync(
                    ts_code=ts_code, direction="sell", volume=remaining,
                    price_type="latest", price=new_price or -1,
                    remark=f"sell_pending_retry{attempt}",
                )
                if new_id > 0:
                    active_orders[ts_code] = {**info, "order_id": new_id, "remaining": remaining}
                    self._log(f"  重新委托 {ts_code} {info['name']} "
                               f"卖出 {remaining}股 {price_label} (order_id={new_id})")
                else:
                    self._log(f"  {ts_code} 重新委托失败 (order_id={new_id})")

            time.sleep(ORDER_RETRY_INTERVAL)

        with self._lock:
            self._state.pending_sell_signals = []
            self._state.sell_signal_execution_date = now.strftime("%Y-%m-%d")
        self._save_pending_signals()

        # ── 清理已卖出的持仓注册表条目（仅清理实际下了卖单的） ──
        for sig in pending:
            ts_code = sig["ts_code"]
            if ts_code in active_orders:
                self._remove_adopted_position(ts_code)

        self._collect_qmt_logs()
        if sell_order_items:
            self._notify_execution_result("早盘卖出", sell_order_items)
        self._log("早盘卖出阶段执行完毕")

    def _fetch_oamv_daily(self, now: datetime.datetime) -> None:
        """拉取当日活跃市值并更新本地 OAMV.XLSX。"""
        self._log(f"─── {OAMV_FETCH_TIME[0]:02d}:{OAMV_FETCH_TIME[1]:02d} 拉取当日活跃市值 ───")
        try:
            from app.trading.oamv_fetcher import OAMVFetcher

            fetcher = OAMVFetcher()
            fetcher.load()
            event = fetcher.fetch_and_update_today()

            df = fetcher._df
            latest = df.iloc[-1]
            self._log(
                f"OAMV={latest['OAMV数值']:.1f}  DIF={latest['DIF']:.3f}  "
                f"DEA={latest['DEA']:.3f}  MACD差值={latest['MACD差值']:.3f}"
            )
            if event:
                self._log(f"检测到 OAMV 事件: {event}")
                oamv_info = (
                    f"OAMV={latest['OAMV数值']:.1f}  DIF={latest['DIF']:.3f}  "
                    f"DEA={latest['DEA']:.3f}  MACD差值={latest['MACD差值']:.3f}"
                )
                self._notify_oamv_event(event, oamv_info)
                self._persist_oamv_event(pd.Timestamp.now().normalize(), event)
            else:
                self._log("当日无 OAMV 事件（金叉/死叉/大涨）")

            oamv_summary = (
                f"OAMV={latest['OAMV数值']:.1f}  DIF={latest['DIF']:.3f}  "
                f"DEA={latest['DEA']:.3f}  MACD差值={latest['MACD差值']:.3f}"
            )
            if event:
                oamv_summary += f"  事件: {event}"

            with self._lock:
                self._state.today_oamv_fetched = True
                self._state.last_oamv_fetch = now.strftime("%Y-%m-%d %H:%M:%S")
                send_afternoon = not self._state.today_afternoon_report_sent
                self._state.today_afternoon_report_sent = True

            if send_afternoon:
                self._send_afternoon_report(oamv_summary)
        except Exception as exc:
            self._log(f"拉取活跃市值失败: {exc}")

    # ────────────── T+1 买入执行 ──────────────

    def _execute_pending_buys(self, now: datetime.datetime) -> None:
        """T+1 早盘执行待买入：9:26 首次委托 → 等待至 9:30 → 撤单重委 → 5 秒轮询。"""
        with self._lock:
            pending = list(self._state.pending_buy_signals)
            config = self._state.config
        if not pending:
            return

        self._log("=" * 50)
        self._log(f"─── 买入阶段（{len(pending)} 只待买入） ───")

        client = get_qmt_client()
        if not client.is_connected:
            self._log("[错误] QMT 未连接，跳过买入执行")
            return

        account_info = client.query_account()
        with self._lock:
            self._state.account_info = account_info

        total_for_strategy = account_info.total_asset * config.fund_ratio
        available_for_strategy = min(account_info.available_cash, total_for_strategy)
        self._log(f"账户可用资金: {account_info.available_cash:,.2f}  "
                   f"策略可用: {available_for_strategy:,.2f}")

        from app.trading.order_generator import _round_to_lot

        # ── 手动买入不受仓位上限约束，先分离 ──
        manual_pending = [s for s in pending if s.get("manual")]
        strategy_pending = [s for s in pending if not s.get("manual")]

        # ── 兜底校验：执行时策略范围内持仓不超限（仅策略买入） ──
        strategy_target = self._state.strategy_target_holdings
        if strategy_target > 0 and strategy_pending:
            qmt_positions = client.query_positions()
            qmt_codes = {p.ts_code for p in qmt_positions if p.volume > 0}
            strategy_codes = set(self._state.strategy_holding_codes)
            pending_buy_codes = {sig["ts_code"] for sig in strategy_pending}
            strategy_current = len(qmt_codes & (strategy_codes - pending_buy_codes))
            max_buys = max(0, strategy_target - strategy_current)
            if max_buys < len(strategy_pending):
                skipped = strategy_pending[max_buys:]
                strategy_pending = strategy_pending[:max_buys]
                for p in skipped:
                    self._log(f"  {p['ts_code']} {p.get('name', '')}: "
                              f"策略内持仓{strategy_current}只，目标{strategy_target}只，"
                              f"可买入{max_buys}只，跳过")

        pending = strategy_pending + manual_pending
        if not pending:
            self._log("持仓已达目标上限且无手动买入，跳过全部买入")
            with self._lock:
                self._state.pending_buy_signals = []
                self._state.buy_execution_date = now.strftime("%Y-%m-%d")
            self._save_pending_signals()
            return

        # ── Phase 1: 首次委托 ──
        active_orders: dict[str, dict] = {}
        buy_order_items: list[OrderItem] = []

        for sig in pending:
            ts_code = sig["ts_code"]
            name = sig.get("name", "")
            price = client.get_latest_price(ts_code)
            if price is None or price <= 0:
                self._log(f"  {ts_code} {name}: 无法获取最新价，跳过")
                continue

            specified_vol = sig.get("volume")
            if specified_vol and int(specified_vol) > 0:
                buy_vol = _round_to_lot(int(specified_vol), ts_code)
                buy_value = buy_vol * price
                if buy_value > available_for_strategy:
                    self._log(f"  {ts_code} {name}: 指定买入 {buy_vol}股 需 {buy_value:,.0f}元 > 可用 {available_for_strategy:,.0f}元，跳过")
                    continue
            else:
                per_stock_value = total_for_strategy * 0.2
                buy_value = min(per_stock_value, available_for_strategy)
                if buy_value <= 0:
                    self._log("  资金不足，跳过剩余买入")
                    break
                buy_vol = _round_to_lot(buy_value / price, ts_code)
            if buy_vol <= 0:
                self._log(f"  {ts_code} {name}: 计算买入数量为 0，跳过")
                continue

            order_id = client.order_stock_sync(
                ts_code=ts_code, direction="buy", volume=buy_vol,
                price_type="latest", price=-1, remark="buy_phase1",
            )
            if order_id > 0:
                active_orders[ts_code] = {
                    "order_id": order_id, "name": name,
                    "volume": buy_vol, "remaining": buy_vol,
                }
                self._log(f"  委托买入 {ts_code} {name} {buy_vol}股 "
                           f"@{price:.2f} (order_id={order_id})")
            else:
                self._log(f"  {ts_code} {name} 下单失败 (order_id={order_id})")

            available_for_strategy -= buy_vol * price

            buy_order_items.append(OrderItem(
                ts_code=ts_code, name=name, direction="buy",
                price=round(price, 3), volume=buy_vol,
                amount=round(buy_vol * price, 2),
                price_type="最新价", remark="T+1买入",
            ))

        with self._lock:
            self._state.today_orders.extend(buy_order_items)

        if not active_orders:
            self._log("无有效买入委托")
            with self._lock:
                self._state.pending_buy_signals = []
                self._state.buy_execution_date = now.strftime("%Y-%m-%d")
            self._save_pending_signals()
            return

        # ── 等待至 9:30 ──
        now_dt = datetime.datetime.now()
        target_930 = now_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_dt < target_930:
            wait_secs = (target_930 - now_dt).total_seconds()
            self._log(f"等待至 9:30 检查委托状态（{wait_secs:.0f}秒）...")
            waited = 0.0
            while waited < wait_secs and not self._stop_event.is_set():
                step = min(5.0, wait_secs - waited)
                time.sleep(step)
                waited += step

        # ── Phase 2: 撤单重新委托轮询 ──
        self._log("─── 9:30 撤单重新委托轮询 ───")
        for attempt in range(BUY_MAX_RETRIES):
            if self._stop_event.is_set():
                break

            still_pending: dict[str, dict] = {}
            for ts_code, info in active_orders.items():
                detail = client.get_order_detail(info["order_id"])
                if detail is None:
                    still_pending[ts_code] = info
                    continue

                status_code = detail.get("order_status_code", 255)
                traded = int(detail.get("traded_volume", 0))

                if status_code == 56 or traded >= info["remaining"]:
                    self._log(f"  {ts_code} {info['name']} 买入已全部成交")
                    continue

                remaining = info["remaining"] - traded
                if remaining <= 0:
                    continue
                still_pending[ts_code] = {**info, "remaining": remaining}

            if not still_pending:
                self._log("所有买入委托已成交")
                break

            for ts_code, info in still_pending.items():
                old_id = info["order_id"]
                remaining = info["remaining"]
                try:
                    client.cancel_order(old_id)
                except Exception as exc:
                    self._log(f"  撤单异常 {ts_code} order_id={old_id}: {exc}")
                time.sleep(1)
                # 撤单后重新查询老委托的最终成交量
                post_cancel = client.get_order_detail(old_id)
                if post_cancel is not None:
                    final_traded = int(post_cancel.get("traded_volume", 0))
                    if final_traded > 0:
                        newly_filled = max(final_traded - (int(info["volume"]) - remaining), 0)
                        remaining = max(remaining - newly_filled, 0)
                        info = {**info, "remaining": remaining}
                self._log(f"  撤单 {ts_code} order_id={old_id}，剩余 {remaining}股")

                if remaining <= 0:
                    self._log(f"  {ts_code} {info['name']} 买入已全部成交（撤单后确认）")
                    active_orders[ts_code] = {**info, "order_id": old_id, "remaining": 0}
                    continue

                new_price = client.get_latest_price(ts_code)
                price_label = f"@{new_price:.2f}" if new_price else "@最新价"
                new_id = client.order_stock_sync(
                    ts_code=ts_code, direction="buy", volume=remaining,
                    price_type="latest", price=new_price or -1,
                    remark=f"buy_retry{attempt}",
                )
                if new_id > 0:
                    active_orders[ts_code] = {**info, "order_id": new_id, "remaining": remaining}
                    self._log(f"  重新委托 {ts_code} {info['name']} "
                               f"{remaining}股 {price_label} (order_id={new_id})")
                else:
                    self._log(f"  {ts_code} 重新委托失败 (order_id={new_id})")

            time.sleep(ORDER_RETRY_INTERVAL)

        with self._lock:
            self._state.pending_buy_signals = []
            self._state.buy_execution_date = now.strftime("%Y-%m-%d")
        self._save_pending_signals()

        # ── 将所有买入加入持仓注册表 ──
        order_price_map = {item.ts_code: item.price for item in buy_order_items}
        for sig in pending:
            if sig["ts_code"] in order_price_map:
                self._add_adopted_position(
                    ts_code=sig["ts_code"],
                    name=sig.get("name", ""),
                    buy_date=now.strftime("%Y-%m-%d"),
                    buy_price=order_price_map[sig["ts_code"]],
                    source="manual" if sig.get("manual") else "strategy",
                )

        self._collect_qmt_logs()
        if buy_order_items:
            self._notify_execution_result("早盘买入", buy_order_items)
        self._log("买入阶段执行完毕")

    # ────────────── 撤单重新委托通用方法 ──────────────

    def _place_with_retry(
        self,
        ts_code: str,
        direction: str,
        volume: int,
        remark: str,
        max_retries: int,
        deadline: tuple[int, int] | None = None,
    ) -> tuple[bool, int]:
        """下单后每 ORDER_RETRY_INTERVAL 秒检查，未成交则撤单重新委托。

        Parameters
        ----------
        deadline : (hour, minute) 截止时间，超过后不再重试（用于下午收盘前卖出）。

        Returns (fully_filled, total_traded_volume)
        """
        client = get_qmt_client()
        remaining = volume
        total_traded = 0

        for attempt in range(max_retries + 1):
            if self._stop_event.is_set() or remaining <= 0:
                break

            if deadline is not None:
                now_hm = (datetime.datetime.now().hour, datetime.datetime.now().minute)
                if now_hm >= deadline:
                    self._log(f"    已过截止时间 {deadline[0]:02d}:{deadline[1]:02d}，"
                               f"停止重试（已成交 {total_traded}股，未成交 {remaining}股）")
                    break

            order_id = client.order_stock_sync(
                ts_code=ts_code, direction=direction, volume=remaining,
                price_type="latest", price=-1,
                remark=f"{remark}_r{attempt}",
            )
            if order_id <= 0:
                self._log(f"    下单失败 (order_id={order_id})")
                time.sleep(ORDER_RETRY_INTERVAL)
                continue

            dir_label = "买入" if direction == "buy" else "卖出"
            self._log(f"    委托 {ts_code} {dir_label} {remaining}股 (order_id={order_id})")
            time.sleep(ORDER_RETRY_INTERVAL)

            detail = client.get_order_detail(order_id)
            if detail:
                traded = int(detail.get("traded_volume", 0))
                status_code = detail.get("order_status_code", 255)
                total_traded += traded
                remaining -= traded

                if status_code == 56 or remaining <= 0:
                    self._log(f"    全部成交 ({total_traded}股)")
                    return True, total_traded

                self._log(f"    已成交 {traded}股，剩余 {remaining}股，撤单重新委托")
                try:
                    client.cancel_order(order_id)
                except Exception:
                    pass
                time.sleep(1)
                # 撤单后重新查询最终成交量
                post_cancel = client.get_order_detail(order_id)
                if post_cancel is not None:
                    final_traded = int(post_cancel.get("traded_volume", 0))
                    extra = final_traded - traded
                    if extra > 0:
                        total_traded += extra
                        remaining -= extra
                        if remaining <= 0:
                            self._log(f"    全部成交（撤单后确认，共 {total_traded}股）")
                            return True, total_traded
            else:
                self._log(f"    无法查询委托状态，撤单重新委托")
                try:
                    client.cancel_order(order_id)
                except Exception:
                    pass
                time.sleep(1)

        if remaining > 0:
            dir_label = "买入" if direction == "buy" else "卖出"
            self._log(f"    达到最大重试({max_retries}次)，"
                       f"已成交 {total_traded}股，未成交 {remaining}股")

        self._collect_qmt_logs()
        return remaining <= 0, total_traded

    def _collect_qmt_logs(self) -> None:
        """收集 QMT 回调日志。"""
        client = get_qmt_client()
        new_logs = client.get_execution_log()
        if new_logs:
            with self._lock:
                self._state.execution_log.extend(new_logs)
                self._state.log_seq += len(new_logs)
                if len(self._state.execution_log) > MAX_MEMORY_LOG_LINES + 100:
                    self._flush_old_logs()

    # ────────────── OAMV 事件持久化 ──────────────

    def _persist_oamv_event(self, trade_date: pd.Timestamp, event: str) -> None:
        """将检测到的 OAMV 事件（金叉/死叉/大涨）追加到 OAMV_MACD.xlsx。

        若当日已有记录则更新，否则追加新行。文件被占用时仅记录警告。
        """
        from pathlib import Path

        oamv_macd_path = Path(__file__).resolve().parents[1] / "analysis" / "OAMV_MACD.xlsx"
        trade_date = pd.Timestamp(trade_date).normalize()

        try:
            if oamv_macd_path.exists():
                df = pd.read_excel(oamv_macd_path)
            else:
                oamv_macd_path.parent.mkdir(parents=True, exist_ok=True)
                df = pd.DataFrame(columns=["日期", "状态"])

            col_date = df.columns[0] if len(df.columns) >= 1 else "日期"
            col_status = df.columns[1] if len(df.columns) >= 2 else "状态"

            df[col_date] = pd.to_datetime(df[col_date], errors="coerce")
            existing_mask = df[col_date] == trade_date

            if existing_mask.any():
                old_status = str(df.loc[existing_mask, col_status].iloc[0]).strip()
                if old_status == event:
                    self._log(f"OAMV_MACD.xlsx 已有 {trade_date.strftime('%Y-%m-%d')} {event}，无需更新")
                    return
                self._log(
                    f"OAMV_MACD.xlsx {trade_date.strftime('%Y-%m-%d')} 已有 [{old_status}]，"
                    f"自动检测到 [{event}]，保留原记录不覆盖"
                )
                return
            else:
                new_row = pd.DataFrame([{col_date: trade_date, col_status: event}])
                df = pd.concat([df, new_row], ignore_index=True)
                self._log(f"OAMV_MACD.xlsx 已追加 {trade_date.strftime('%Y-%m-%d')} {event}")

            df = df.sort_values(col_date).reset_index(drop=True)

            tmp_path = oamv_macd_path.with_suffix(".tmp.xlsx")
            df.to_excel(tmp_path, index=False)
            tmp_path.replace(oamv_macd_path)

        except PermissionError:
            self._log(f"[警告] OAMV_MACD.xlsx 被占用，无法写入事件 {event}。请关闭 Excel 后手动补录。")
        except Exception as exc:
            self._log(f"[警告] 写入 OAMV_MACD.xlsx 失败: {exc}")

    # ────────────── 飞书通知 ──────────────

    def _notify_buy_signals(self, pending: list[dict]) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")
        title = f"\U0001F4C8 买入信号 ({len(pending)}只) - {today}"
        sections = [build_line(f"\U0001F4CB 共选出 {len(pending)} 只待买入标的：")]
        sections.append(build_divider())
        for i, p in enumerate(pending, 1):
            sections.append(build_line(f"  {i}. {p['ts_code']}  {p.get('name', '')}"))
        sections.append(build_divider())
        sections.append(build_line("\u23F0 将于下一交易日 9:26 集合竞价委托"))
        send_feishu_notification(title, sections)

    def _notify_no_buy_signal(self, holdings_count: int, max_holdings: int, strategy_warnings: list[str]) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")
        title = f"\U0001F4AD 无买入信号 - {today}"
        sections = [build_line("\U0001F50D 买入信号扫描完成，未发现可买入标的")]
        sections.append(build_divider())
        if holdings_count >= max_holdings:
            sections.append(build_line(f"\U0001F4BC 原因：已满仓 ({holdings_count}/{max_holdings} 只)"))
        else:
            sections.append(build_line(f"\U0001F4BC 当前持仓：{holdings_count}/{max_holdings} 只，仍有空仓位"))
            sections.append(build_line("\U0001F6AB 原因：无股票满足选股条件（评分/趋势/KDJ/量价等过滤）"))
        if strategy_warnings:
            sections.append(build_divider())
            sections.append(build_line("\u26A0\uFE0F 策略警告："))
            for w in strategy_warnings[-5:]:
                sections.append(build_line(f"  \u2022 {w}"))
        send_feishu_notification(title, sections)

    def _notify_sell_signals(self, pending: list[dict]) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")
        title = f"\U0001F4C9 卖出信号 ({len(pending)}只) - {today}"
        sections = [build_line(f"\U0001F4CB 发现 {len(pending)} 只待卖出：")]
        sections.append(build_divider())
        for i, p in enumerate(pending, 1):
            reason = p.get("reason", "")
            sections.append(build_line(f"  {i}. {p['ts_code']}  {p.get('name', '')}"))
            sections.append(build_line(f"      \U0001F4CC {reason}"))
        sections.append(build_divider())
        sections.append(build_line("\u23F0 将于下一交易日 9:26 委托卖出"))
        send_feishu_notification(title, sections)

    def _notify_no_sell_signal(self, already_sold_count: int) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")
        title = f"\u2705 卖出信号扫描完成 - {today}"
        sections = [build_line("\U0001F50D 盘后卖出信号扫描完成")]
        sections.append(build_divider())
        if already_sold_count > 0:
            sections.append(build_line(f"\U0001F4BC 盘中已卖出 {already_sold_count} 只"))
        sections.append(build_line("\u2705 无遗漏卖出信号，持仓无需变动"))
        send_feishu_notification(title, sections)

    def _notify_sell_phase(self, sell_signals: list[dict], name_map: dict[str, str]) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")
        title = f"\U0001F51B 盘中卖出 ({len(sell_signals)}只) - {today}"
        sections = [build_line(f"\U0001F4CB 策略触发 {len(sell_signals)} 只卖出信号：")]
        sections.append(build_divider())
        for i, sig in enumerate(sell_signals, 1):
            ts_code = sig["ts_code"]
            name = name_map.get(ts_code, "")
            reason = sig.get("reason", "策略卖出")
            sections.append(build_line(f"  {i}. {ts_code}  {name}"))
            sections.append(build_line(f"      \U0001F4CC {reason}"))
        sections.append(build_divider())
        sections.append(build_line("\u26A1 正在执行委托..."))
        send_feishu_notification(title, sections)

    def _notify_no_sell_phase(self) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line
        today = datetime.date.today().strftime("%Y-%m-%d")
        title = f"\u2705 盘中无卖出 - {today}"
        sections = [build_line("\U0001F50D 14:53 盘中卖出检查完成")]
        sections.append(build_line("\u2705 无持仓触发止损/止盈/调仓条件"))
        send_feishu_notification(title, sections)

    def _notify_oamv_event(self, event: str, oamv_info: str) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")
        icon = {"\u91d1\u53c9": "\U0001F7E2", "\u6b7b\u53c9": "\U0001F534", "\u5927\u6da8": "\U0001F525"}.get(event, "\u26A0\uFE0F")
        title = f"{icon} OAMV 事件: {event} - {today}"
        sections = [
            build_line(f"{icon} 检测到活跃市值 {event} 事件"),
            build_divider(),
            build_line(f"\U0001F4CA {oamv_info}"),
        ]
        send_feishu_notification(title, sections)

    def _notify_execution_result(self, phase: str, orders: list[OrderItem]) -> None:
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")
        status_icons = {
            "filled": "\u2705", "partial": "\u26A0\uFE0F",
            "failed": "\u274C", "submitted": "\u23F3",
        }
        status_labels = {
            "filled": "\u5DF2\u6210\u4EA4", "partial": "\u90E8\u5206\u6210\u4EA4",
            "failed": "\u5931\u8D25", "submitted": "\u5DF2\u59D4\u6258",
        }
        filled = sum(1 for o in orders if o.status == "filled")
        total = len(orders)
        result_icon = "\u2705" if filled == total else ("\u26A0\uFE0F" if filled > 0 else "\u274C")
        title = f"{result_icon} {phase}完成 ({filled}/{total}\u6210\u4EA4) - {today}"
        sections = [build_line(f"\U0001F4CB {phase}执行结果：")]
        sections.append(build_divider())
        for o in orders:
            icon = status_icons.get(o.status, "\u2753")
            label = status_labels.get(o.status, o.status)
            dir_label = "\u4E70\u5165" if o.direction == "buy" else "\u5356\u51FA"
            price_str = f" @{o.price:.2f}" if o.price > 0 else ""
            sections.append(build_line(
                f"  {icon} {o.ts_code} {o.name}  {dir_label}{o.volume}\u80A1{price_str}  {label}"
            ))
        send_feishu_notification(title, sections)

    def _send_morning_report(self) -> None:
        """早盘 9:35 持仓报告。"""
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")

        client = get_qmt_client()
        if not client.is_connected:
            return
        try:
            account_info = client.query_account()
            positions = client.query_positions()
        except Exception:
            return
        for p in positions:
            if not p.name and p.ts_code in self._name_cache:
                p.name = self._name_cache[p.ts_code]
        with self._lock:
            self._state.account_info = account_info
            self._state.positions = positions

        sections = self._build_position_report_sections(account_info, positions)
        title = f"\U0001F4CA \u65E9\u76D8\u6301\u4ED3\u62A5\u544A - {today}"
        send_feishu_notification(title, sections)
        self._log("已发送早盘持仓报告")

    def _send_afternoon_report(self, oamv_summary: str) -> None:
        """盘后持仓报告（含活跃市值）。"""
        from app.trading.feishu_notifier import send_feishu_notification, build_line, build_divider
        today = datetime.date.today().strftime("%Y-%m-%d")

        client = get_qmt_client()
        if not client.is_connected:
            return
        try:
            account_info = client.query_account()
            positions = client.query_positions()
        except Exception:
            return
        for p in positions:
            if not p.name and p.ts_code in self._name_cache:
                p.name = self._name_cache[p.ts_code]
        with self._lock:
            self._state.account_info = account_info
            self._state.positions = positions

        sections = self._build_position_report_sections(account_info, positions)
        sections.append(build_divider())
        sections.append(build_line(f"\U0001F4C8 \u6D3B\u8DC3\u5E02\u503C\uFF1A{oamv_summary}"))
        title = f"\U0001F4CA \u76D8\u540E\u6301\u4ED3\u62A5\u544A - {today}"
        send_feishu_notification(title, sections)
        self._log("已发送盘后持仓报告")

    def _build_position_report_sections(
        self, account_info: AccountInfo, positions: list[PositionItem],
    ) -> list:
        from app.trading.feishu_notifier import build_line, build_divider
        total_asset = account_info.total_asset
        available_cash = account_info.available_cash
        market_value = account_info.market_value
        held = [p for p in positions if p.volume > 0]
        pos_pct = f"{market_value / total_asset * 100:.1f}%" if total_asset > 0 else "0%"

        sections = [
            build_line(f"\U0001F4B0 \u603B\u8D44\u4EA7\uFF1A{total_asset:,.2f}"),
            build_line(f"\U0001F4BC \u6301\u4ED3\u5E02\u503C\uFF1A{market_value:,.2f}  |  "
                       f"\u53EF\u7528\u8D44\u91D1\uFF1A{available_cash:,.2f}"),
            build_line(f"\U0001F4CA \u4ED3\u4F4D\u5360\u6BD4\uFF1A{pos_pct}  |  "
                       f"\u6301\u4ED3 {len(held)} \u53EA"),
        ]

        if held:
            sections.append(build_divider())
            total_profit = 0.0
            for p in held:
                weight = p.market_value / total_asset * 100 if total_asset > 0 else 0
                icon = "\U0001F7E2" if p.profit >= 0 else "\U0001F534"
                rate_str = f"+{p.profit_rate * 100:.2f}%" if p.profit_rate >= 0 else f"{p.profit_rate * 100:.2f}%"
                pnl_str = f"+{p.profit:,.2f}" if p.profit >= 0 else f"{p.profit:,.2f}"
                sections.append(build_line(
                    f"  {icon} {p.ts_code} {p.name}"
                ))
                sections.append(build_line(
                    f"      {p.volume}\u80A1  \u4ED3\u4F4D{weight:.1f}%  "
                    f"\u6536\u76CA{rate_str}  {pnl_str}"
                ))
                total_profit += p.profit
            sections.append(build_divider())
            pnl_icon = "\U0001F4B9" if total_profit >= 0 else "\U0001F4C9"
            pnl_label = f"+{total_profit:,.2f}" if total_profit >= 0 else f"{total_profit:,.2f}"
            sections.append(build_line(f"{pnl_icon} \u6301\u4ED3\u603B\u6536\u76CA\uFF1A{pnl_label}"))
        else:
            sections.append(build_divider())
            sections.append(build_line("\U0001F4AD \u5F53\u524D\u65E0\u6301\u4ED3"))

        return sections

    # ────────────── 持仓对齐 & 资金变动检测 ──────────────

    _last_known_total_asset: float = 0.0

    def _log_position_reconciliation(
        self,
        strategy_holdings: list[dict],
        qmt_held_codes: set[str],
        qmt_pos_map: dict[str, Any],
        sell_signals: list[dict] | None = None,
        buy_signals: list[dict] | None = None,
    ) -> None:
        """对比策略目标持仓与 QMT 实际持仓，打印差异日志。

        结合 sell/buy 信号提供上下文感知的描述，避免误导性的"不干预"等提示。
        """
        strategy_codes = {h.get("ts_code", "") for h in strategy_holdings if h.get("ts_code")}
        sell_codes = {s["ts_code"] for s in (sell_signals or [])}
        buy_codes = {b["ts_code"] for b in (buy_signals or [])}

        phantom = strategy_codes - qmt_held_codes
        external = qmt_held_codes - strategy_codes
        matched = strategy_codes & qmt_held_codes

        if not phantom and not external:
            self._log(f"持仓对齐: 策略与 QMT 一致 ({len(matched)} 只)")
            return

        self._log(f"持仓对齐: 策略 {len(strategy_codes)} 只 vs QMT {len(qmt_held_codes)} 只 "
                   f"(一致 {len(matched)}, 差异 {len(phantom) + len(external)})")
        for code in sorted(phantom):
            if code in buy_codes:
                self._log(f"  [待买入] {code}: 策略新选股，QMT 尚未持有 → 已产生买入信号")
            else:
                self._log(f"  [幻影持仓] {code}: 策略认为持有但 QMT 已无 → 已释放仓位供新选股")
        for code in sorted(external):
            pos = qmt_pos_map.get(code)
            vol = pos.volume if pos else 0
            if code in sell_codes:
                self._log(f"  [待卖出] {code}: QMT 持有 {vol} 股，策略已产生卖出信号")
            else:
                self._log(f"  [外部持仓] {code}: QMT 持有 {vol} 股但不在策略管理范围内 → 不干预")

    def _detect_fund_change(self, account_info: AccountInfo) -> None:
        """检测资金余额变化并记录日志（提示手动转入/转出）。"""
        prev = self._last_known_total_asset
        curr = account_info.total_asset
        if prev > 0 and abs(curr - prev) > 1.0:
            diff = curr - prev
            pct = diff / prev * 100
            direction = "增加" if diff > 0 else "减少"
            if abs(pct) >= 1.0:
                self._log(f"[资金变动] 总资产 {prev:,.2f} → {curr:,.2f} "
                           f"({direction} {abs(diff):,.2f}, {abs(pct):.2f}%)")
                if abs(pct) >= 5.0:
                    self._log(f"  提示: 资金大幅{direction}，"
                               f"可能是手动{'转入' if diff > 0 else '转出'}或持仓盈亏波动")
        self.__class__._last_known_total_asset = curr

    def _inject_realtime_data(
        self,
        market_data: pd.DataFrame,
        trade_dates: list[pd.Timestamp],
        today_ts: pd.Timestamp,
        realtime_prices: dict[str, dict],
    ) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
        """将实时行情注入 market_data 作为今日数据行。"""
        existing_codes = set(market_data["ts_code"].unique())

        last_rows = market_data.groupby("ts_code").last()

        new_rows: list[dict] = []
        for ts_code, rt in realtime_prices.items():
            if ts_code not in existing_codes:
                continue
            last_row = last_rows.loc[ts_code]
            new_rows.append({
                "ts_code": ts_code,
                "trade_date": today_ts,
                "name": rt.get("name", last_row.get("name", "")),
                "industry": last_row.get("industry", "未知"),
                "open": rt.get("open", 0),
                "high": rt.get("high", 0),
                "low": rt.get("low", 0),
                "close": rt.get("close", rt.get("latest_price", 0)),
                "vol": rt.get("vol", 0),
                "amount": rt.get("amount", 0),
                "circ_mv": last_row.get("circ_mv", 0),
            })

        if not new_rows:
            return market_data, trade_dates

        new_df = pd.DataFrame(new_rows)
        market_data = pd.concat([market_data, new_df], ignore_index=True)

        if today_ts not in trade_dates:
            trade_dates = list(trade_dates) + [today_ts]

        return market_data, trade_dates


_scheduler_instance: TradingScheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> TradingScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        with _scheduler_lock:
            if _scheduler_instance is None:
                _scheduler_instance = TradingScheduler()
    return _scheduler_instance
