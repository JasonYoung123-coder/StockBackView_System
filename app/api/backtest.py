from __future__ import annotations

from threading import Thread

import pandas as pd
from fastapi import APIRouter, HTTPException

from app.backtest.engine import BacktestEngine
from app.backtest.models import (
    BacktestJobCreateResponse,
    BacktestJobStatusResponse,
    DailyHoldingDetail,
    DailyPositionDetail,
    BacktestRequest,
    BacktestResponse,
    CurvePoint,
    CurveSeries,
    StrategyInfo,
    TradeRecord,
    TradeSummary,
)
from app.core.config import ConfigError, get_settings
from app.services.backtest_jobs import job_store
from app.services.benchmark_service import BenchmarkService
from app.services.market_data_service import MarketDataService
from app.services.tushare_client import TushareClient
from app.strategy.loader import StrategyLoadError, StrategyLoader

router = APIRouter()


@router.get("/api/strategies")
async def list_strategies() -> list[StrategyInfo]:
    loader = StrategyLoader()
    try:
        strategies = loader.list_strategies()
    except StrategyLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        StrategyInfo(
            name=item.name,
            description=item.description or "未提供描述",
            path=str(item.path),
            adapted=item.adapted,
            config_schema=item.config_schema,
        )
        for item in strategies
    ]


@router.post("/api/backtest/jobs", response_model=BacktestJobCreateResponse)
async def create_backtest_job(payload: BacktestRequest) -> BacktestJobCreateResponse:
    job = job_store.create()
    job_store.update(job.job_id, status="running", progress=1, message="回测任务已启动")
    thread = Thread(target=_run_backtest_job, args=(job.job_id, payload), daemon=True)
    thread.start()
    return BacktestJobCreateResponse(job_id=job.job_id, status="running", progress=1, message="回测任务已启动")


@router.get("/api/backtest/jobs/{job_id}", response_model=BacktestJobStatusResponse)
async def get_backtest_job(job_id: str) -> BacktestJobStatusResponse:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="未找到对应的回测任务。")
    return BacktestJobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        result=job.result,
        error=job.error,
    )


def _run_backtest_job(job_id: str, payload: BacktestRequest) -> None:
    try:
        result = _execute_backtest(
            payload,
            progress_callback=lambda progress, message: job_store.update(
                job_id,
                status="running",
                progress=progress,
                message=message,
            ),
        )
        job_store.update(job_id, status="completed", progress=100, message="回测完成", result=result)
    except Exception as exc:
        job_store.update(job_id, status="failed", progress=100, message="回测失败", error=str(exc))


def _execute_backtest(payload: BacktestRequest, progress_callback=None) -> BacktestResponse:
    settings = get_settings()
    loader = StrategyLoader()
    tushare_client = TushareClient()
    benchmark_service = BenchmarkService(tushare_client=tushare_client)
    market_data_service = MarketDataService(tushare_client=tushare_client)
    engine = BacktestEngine()

    try:
        _update_progress(progress_callback, 5, "正在加载策略")
        strategy = loader.get_strategy(payload.strategy_name, params=payload.strategy_params)
        strategy.instance.set_progress_callback(progress_callback)
        _update_progress(progress_callback, 12, "正在准备市场数据")
        market_data, trade_dates = market_data_service.get_market_history(
            payload.start_date.strftime("%Y%m%d"),
            payload.end_date.strftime("%Y%m%d"),
            lookback_days=getattr(strategy.instance, "lookback_days", 80),
            progress_callback=progress_callback,
        )
        if market_data.empty or not trade_dates:
            raise HTTPException(status_code=400, detail="市场数据为空，无法回测。")

        _update_progress(progress_callback, 50, "正在加载基准指数")
        benchmark_data = benchmark_service.get_benchmarks(
            payload.start_date.strftime("%Y%m%d"),
            payload.end_date.strftime("%Y%m%d"),
            base_dates=pd.DatetimeIndex(trade_dates),
        )
        _update_progress(progress_callback, 58, "正在执行回测计算")
        result = engine.run(
            market_data=market_data,
            strategy=strategy.instance,
            trade_dates=trade_dates,
            benchmark_data=benchmark_data,
            initial_capital=payload.initial_capital,
            commission_rate=payload.commission_rate if payload.commission_rate is not None else settings.default_commission_rate,
            stamp_duty_rate=payload.stamp_duty_rate if payload.stamp_duty_rate is not None else settings.default_stamp_duty_rate,
        )
        _update_progress(progress_callback, 95, "正在整理回测结果")
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StrategyLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    curves = [CurveSeries(name=f"策略: {strategy.name}", points=_curve_points(result.strategy_curve))]
    for name, frame in result.benchmark_curves.items():
        curves.append(CurveSeries(name=name, points=_curve_points(frame)))

    return BacktestResponse(
        strategy=StrategyInfo(
            name=strategy.name,
            description=strategy.description or "未提供描述",
            path=str(strategy.path),
            adapted=strategy.adapted,
            config_schema=strategy.config_schema,
        ),
        asset="全市场选股组合",
        start_date=payload.start_date.isoformat(),
        end_date=payload.end_date.isoformat(),
        curves=curves,
        metrics=result.metrics,
        signal_summary=result.signal_summary,
        trade_summary=TradeSummary(**result.trade_summary),
        trade_records=[TradeRecord(**item) for item in result.trade_records],
        daily_position_details=[
            DailyPositionDetail(
                **{
                    **item,
                    "holdings": [DailyHoldingDetail(**holding) for holding in item.get("holdings", [])],
                }
            )
            for item in result.daily_position_details
        ],
    )


def _curve_points(frame: pd.DataFrame) -> list[CurvePoint]:
    return [
        CurvePoint(
            date=row.trade_date.strftime("%Y-%m-%d"),
            value=round(float(row.net_value), 6),
            capital=round(float(getattr(row, "capital", 0.0)), 2) if hasattr(row, "capital") else None,
            position=round(float(getattr(row, "position", 0.0)), 6) if hasattr(row, "position") else None,
            daily_return=round(float(getattr(row, "daily_return", 0.0)), 6) if hasattr(row, "daily_return") else None,
            holding_count=int(getattr(row, "holding_count", 0)) if hasattr(row, "holding_count") else None,
        )
        for row in frame.itertuples()
    ]


def _update_progress(callback, progress: float, message: str) -> None:
    if callable(callback):
        callback(progress, message)
