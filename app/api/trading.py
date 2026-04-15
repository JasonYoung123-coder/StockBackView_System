"""QMT 实盘交易 REST API。"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

import pandas as pd
from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.services.market_data_service import MarketDataService
from app.services.tushare_client import TushareClient
from app.strategy.loader import StrategyLoader
from app.trading.models import (
    AccountInfo,
    OrderItem,
    PositionItem,
    SchedulerStartRequest,
    SchedulerStatusResponse,
    TradingAccountResponse,
    TradingJobStatus,
    TradingRunRequest,
)
from app.trading.order_generator import generate_orders
from app.trading.qmt_client import QMTClientError, get_qmt_client
from app.trading.scheduler import SchedulerConfig, get_scheduler

router = APIRouter()


# ── Trading Job Store（复用回测相同模式） ──────────────


@dataclass
class TradingJob:
    job_id: str
    status: str = "pending"
    progress: float = 0.0
    message: str = ""
    account_info: AccountInfo | None = None
    current_positions: list[PositionItem] = field(default_factory=list)
    target_weights: dict[str, float] = field(default_factory=dict)
    orders: list[OrderItem] = field(default_factory=list)
    execution_log: list[str] = field(default_factory=list)
    error: str | None = None


class _TradingJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, TradingJob] = {}
        self._lock = Lock()

    def create(self) -> TradingJob:
        with self._lock:
            job = TradingJob(job_id=str(uuid4()))
            self._jobs[job.job_id] = job
            return job

    def get(self, job_id: str) -> TradingJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in kwargs.items():
                if hasattr(job, key) and value is not None:
                    setattr(job, key, value)
            job.progress = max(0.0, min(100.0, float(job.progress)))


_trading_jobs = _TradingJobStore()


# ── API 端点 ─────────────────────────────────


@router.post("/api/trading/connect")
async def connect_qmt() -> dict[str, Any]:
    settings = get_settings()
    client = get_qmt_client()
    if client.is_connected:
        return {"connected": True, "message": "已连接"}
    try:
        client.connect(
            xtquant_path=settings.qmt_xtquant_path,
            userdata_path=settings.qmt_userdata_path,
            account_id=settings.qmt_account_id,
            account_type=settings.qmt_account_type,
        )
    except QMTClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"connected": True, "message": "连接成功"}


@router.get("/api/trading/account", response_model=TradingAccountResponse)
async def get_account() -> TradingAccountResponse:
    client = get_qmt_client()
    if not client.is_connected:
        return TradingAccountResponse(connected=False)
    try:
        account_info = client.query_account()
        positions = client.query_positions()
    except QMTClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TradingAccountResponse(
        connected=True,
        account_info=account_info,
        positions=positions,
    )


@router.post("/api/trading/run")
async def run_trading(payload: TradingRunRequest) -> dict[str, str]:
    client = get_qmt_client()
    if not client.is_connected:
        raise HTTPException(status_code=400, detail="QMT 未连接，请先点击连接")

    job = _trading_jobs.create()
    _trading_jobs.update(job.job_id, status="running", progress=1, message="交易任务已启动")

    thread = Thread(target=_run_trading_job, args=(job.job_id, payload), daemon=True)
    thread.start()
    return {"job_id": job.job_id, "status": "running"}


@router.get("/api/trading/jobs/{job_id}", response_model=TradingJobStatus)
async def get_trading_job(job_id: str) -> TradingJobStatus:
    job = _trading_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="未找到对应的交易任务")

    # 追加 QMT 回调日志
    client = get_qmt_client()
    new_logs = client.get_execution_log()
    if new_logs:
        job.execution_log.extend(new_logs)

    return TradingJobStatus(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        account_info=job.account_info,
        current_positions=job.current_positions,
        target_weights=job.target_weights,
        orders=job.orders,
        execution_log=job.execution_log,
        error=job.error,
    )


@router.get("/api/trading/orders")
async def get_qmt_orders() -> dict[str, Any]:
    """主动查询 QMT 当日全部委托记录。"""
    client = get_qmt_client()
    if not client.is_connected:
        raise HTTPException(status_code=400, detail="QMT 未连接")
    try:
        orders = client.query_orders()
    except QMTClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"orders": orders}


@router.get("/api/trading/logs")
async def get_qmt_logs() -> dict[str, Any]:
    """获取 QMT 全部回调日志（不依赖 job，连接后即可查看）。"""
    client = get_qmt_client()
    logs = client.get_all_logs()
    return {"logs": logs}


@router.post("/api/trading/disconnect")
async def disconnect_qmt() -> dict[str, Any]:
    scheduler = get_scheduler()
    if scheduler.state.running:
        scheduler.stop()
    client = get_qmt_client()
    client.disconnect()
    return {"connected": False, "message": "已断开连接"}


# ── 调度器端点 ──────────────────────────────


@router.post("/api/trading/scheduler/start")
async def start_scheduler(payload: SchedulerStartRequest) -> dict[str, Any]:
    client = get_qmt_client()
    if not client.is_connected:
        raise HTTPException(status_code=400, detail="QMT 未连接，请先点击连接")

    scheduler = get_scheduler()
    if scheduler.state.running:
        return {"running": True, "message": "调度器已在运行中"}

    config = SchedulerConfig(
        strategy_name=payload.strategy_name,
        fund_ratio=payload.fund_ratio,
        buy_existing=payload.buy_existing,
        allow_sell=payload.allow_sell,
        price_type=payload.price_type,
        lookback_days=payload.lookback_days,
        live_start_date=payload.live_start_date,
    )
    scheduler.start(config)
    return {"running": True, "message": "策略调度已开启"}


@router.post("/api/trading/scheduler/stop")
async def stop_scheduler() -> dict[str, Any]:
    scheduler = get_scheduler()
    scheduler.stop()
    return {"running": False, "message": "策略调度已关闭"}


@router.get("/api/trading/scheduler/status", response_model=SchedulerStatusResponse)
async def get_scheduler_status() -> SchedulerStatusResponse:
    scheduler = get_scheduler()
    s = scheduler.state

    client = get_qmt_client()
    new_logs = client.get_execution_log()
    if new_logs:
        s.execution_log.extend(new_logs)

    from app.trading.models import PendingBuyItem, PendingSellItem

    pending_buy_items = [
        PendingBuyItem(ts_code=p.get("ts_code", ""), name=p.get("name", ""))
        for p in (s.pending_buy_signals or [])
    ]
    pending_sell_items = [
        PendingSellItem(ts_code=p.get("ts_code", ""), name=p.get("name", ""), reason=p.get("reason", ""))
        for p in (s.pending_sell_signals or [])
    ]
    return SchedulerStatusResponse(
        running=s.running,
        strategy_name=s.config.strategy_name,
        fund_ratio=s.config.fund_ratio,
        buy_existing=s.config.buy_existing,
        allow_sell=s.config.allow_sell,
        last_execution=s.last_execution,
        next_execution=s.next_execution,
        today_executed=s.today_executed,
        today_orders=s.today_orders,
        execution_log=s.execution_log,
        account_info=s.account_info,
        positions=s.positions,
        pending_sell_signals=pending_sell_items,
        pending_buy_signals=pending_buy_items,
        error=s.error,
    )


# ── 后台执行逻辑 ─────────────────────────────


def _run_trading_job(job_id: str, payload: TradingRunRequest) -> None:
    try:
        _execute_trading(job_id, payload)
    except Exception as exc:
        _trading_jobs.update(
            job_id, status="failed", progress=100, message="交易执行失败", error=str(exc)
        )


def _execute_trading(job_id: str, payload: TradingRunRequest) -> None:
    client = get_qmt_client()
    settings = get_settings()
    loader = StrategyLoader()
    tushare_client = TushareClient()
    market_service = MarketDataService(tushare_client=tushare_client)

    def _log(msg: str) -> None:
        job = _trading_jobs.get(job_id)
        if job:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            job.execution_log.append(f"[{ts}] {msg}")

    # 1. 加载策略
    _trading_jobs.update(job_id, progress=5, message="正在加载策略")
    _log(f"加载策略: {payload.strategy_name}")
    strategy = loader.get_strategy(payload.strategy_name, params=payload.strategy_params)

    # 2. 获取市场数据
    _trading_jobs.update(job_id, progress=10, message="正在获取市场数据")
    today = datetime.date.today()
    lookback_start = today - datetime.timedelta(days=int(payload.lookback_days * 1.6))
    _log(f"获取市场数据: {lookback_start} ~ {today}")

    market_data, trade_dates = market_service.get_market_history(
        lookback_start.strftime("%Y%m%d"),
        today.strftime("%Y%m%d"),
        lookback_days=0,
    )
    if market_data.empty or not trade_dates:
        raise RuntimeError("市场数据为空，无法执行策略")

    # 3. 运行策略获取目标权重
    _trading_jobs.update(job_id, progress=30, message="正在运行策略计算目标仓位")
    _log("开始运行策略...")
    target_weights_df, strategy_meta = strategy.instance.generate_portfolio_weights(
        market_data, trade_dates
    )
    if target_weights_df.empty:
        raise RuntimeError("策略未生成任何持仓信号")

    last_date = target_weights_df.index[-1]
    last_weights = target_weights_df.iloc[-1]
    target_weights = {
        code: float(w) for code, w in last_weights.items() if float(w) > 0
    }
    _log(f"策略目标仓位 ({last_date.strftime('%Y-%m-%d')}): {len(target_weights)} 只股票")
    for code, w in sorted(target_weights.items(), key=lambda x: -x[1]):
        _log(f"  {code}: {w:.1%}")
    _trading_jobs.update(job_id, target_weights=target_weights)

    # 4. 查询 QMT 账户和持仓
    _trading_jobs.update(job_id, progress=50, message="正在查询账户和持仓")
    account_info = client.query_account()
    positions = client.query_positions()
    _trading_jobs.update(job_id, account_info=account_info, current_positions=positions)
    _log(f"账户总资产: {account_info.total_asset:,.2f}  可用资金: {account_info.available_cash:,.2f}")
    _log(f"当前持仓: {len(positions)} 只")

    # 5. 获取实时价格
    _trading_jobs.update(job_id, progress=60, message="正在获取实时行情")
    all_codes = list(set(target_weights.keys()) | {p.ts_code for p in positions})
    prices = client.get_realtime_prices(all_codes)
    _log(f"获取 {len(prices)} 只股票实时价格")

    # 构建名称映射
    name_map: dict[str, str] = {}
    if not market_data.empty and "name" in market_data.columns:
        dedup = market_data.drop_duplicates(subset=["ts_code"], keep="last")
        name_map = dict(zip(dedup["ts_code"].astype(str), dedup["name"].astype(str)))

    # 6. 生成订单
    _trading_jobs.update(job_id, progress=70, message="正在生成订单")
    orders = generate_orders(
        target_weights=target_weights,
        positions=positions,
        account=account_info,
        prices=prices,
        price_type=payload.price_type,
        name_map=name_map,
    )
    _trading_jobs.update(job_id, orders=orders)
    _log(f"生成 {len(orders)} 笔订单 (卖出 {sum(1 for o in orders if o.direction == 'sell')}, "
         f"买入 {sum(1 for o in orders if o.direction == 'buy')})")

    if not orders:
        _trading_jobs.update(
            job_id, status="completed", progress=100, message="无需调仓，当前持仓已符合策略目标"
        )
        return

    # 7. 逐笔执行
    _trading_jobs.update(job_id, progress=80, message="正在执行下单")
    for idx, order in enumerate(orders):
        dir_label = "买入" if order.direction == "buy" else "卖出"
        _log(f"下单 [{idx + 1}/{len(orders)}] {dir_label} {order.ts_code} {order.name} "
             f"{order.volume}股 @ {order.price_type}")
        try:
            client.order_stock(
                ts_code=order.ts_code,
                direction=order.direction,
                volume=order.volume,
                price_type=payload.price_type,
                price=order.price,
                remark=f"{strategy.name}_{order.direction}",
            )
            order.status = "submitted"
            _log(f"  -> 已提交")
        except Exception as exc:
            order.status = "failed"
            order.remark = str(exc)
            _log(f"  -> 失败: {exc}")

        _trading_jobs.update(
            job_id,
            orders=orders,
            progress=80 + (idx + 1) / len(orders) * 15,
        )

    # 8. 等待 QMT 回调到达，分批收集
    _trading_jobs.update(job_id, progress=96, message="等待 QMT 回报...")
    _log("全部订单已提交，等待 QMT 回调...")
    for wait_round in range(6):
        time.sleep(1)
        new_logs = client.get_execution_log()
        if new_logs:
            job = _trading_jobs.get(job_id)
            if job:
                job.execution_log.extend(new_logs)

    # 9. 主动查询当日委托状态作为兜底
    try:
        qmt_orders = client.query_orders()
        if qmt_orders:
            _log(f"--- QMT 当日委托记录 ({len(qmt_orders)}笔) ---")
            for qo in qmt_orders:
                status = qo.get("order_status", "未知")
                code = qo.get("stock_code", "")
                otype = qo.get("order_type", "")
                vol = qo.get("order_volume", 0)
                traded = qo.get("traded_volume", 0)
                traded_p = qo.get("traded_price", 0)
                msg = qo.get("status_msg", "")
                line = f"  {code} {otype} {vol}股  状态={status}"
                if traded:
                    line += f"  已成={traded}股@{traded_p}"
                if msg:
                    line += f"  {msg}"
                _log(line)
    except Exception as exc:
        _log(f"查询 QMT 委托记录异常: {exc}")

    _trading_jobs.update(
        job_id,
        status="completed",
        progress=100,
        message=f"交易执行完成，共 {len(orders)} 笔订单",
    )
    _log("交易执行完毕")
