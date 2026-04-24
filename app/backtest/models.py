from __future__ import annotations

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class BacktestRequest(BaseModel):
    strategy_name: str = Field(..., description="策略名称")
    start_date: date
    end_date: date
    initial_capital: float = Field(100000, gt=0, description="初始资金")
    commission_rate: Optional[float] = Field(None, ge=0, description="手续费率")
    stamp_duty_rate: Optional[float] = Field(None, ge=0, description="印花税率")
    strategy_params: dict[str, Any] = Field(default_factory=dict, description="策略自定义参数")

    @field_validator("strategy_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_dates(self) -> "BacktestRequest":
        if self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期。")
        return self


class CurvePoint(BaseModel):
    date: str
    value: float
    capital: Optional[float] = None
    position: Optional[float] = None
    daily_return: Optional[float] = None
    holding_count: Optional[int] = None


class CurveSeries(BaseModel):
    name: str
    points: list[CurvePoint]


class DailyHoldingDetail(BaseModel):
    ts_code: str
    name: str
    position_weight: float
    close_price: float
    daily_return: float
    total_return: float
    daily_pnl_amount: float
    floating_pnl_amount: float
    buy_date: str
    buy_price: float


class DailyPositionDetail(BaseModel):
    date: str
    capital: float
    position: float
    holding_count: int
    daily_return: float
    net_value: float
    holdings: list[DailyHoldingDetail]


class MetricSummary(BaseModel):
    total_return: float
    annualized_return: float
    max_drawdown: float
    volatility: float
    sharpe_ratio: float
    win_rate: float
    final_value: float


class StrategyInfo(BaseModel):
    name: str
    description: str
    path: str
    adapted: bool = False
    config_schema: Optional[dict[str, Any]] = None


class TradeRecord(BaseModel):
    trade_no: int
    trade_type: str
    ts_code: str
    name: str
    buy_date: str
    sell_date: str
    sell_reason: str
    buy_price: float
    sell_price: float
    position_weight: float
    shares: float
    buy_amount: float
    sell_amount: float
    pnl_amount: float
    return_rate: float
    holding_days: int


class TradeSummary(BaseModel):
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl_amount: float
    average_pnl_amount: float
    average_return_rate: float
    best_trade_return: float
    worst_trade_return: float


class BacktestResponse(BaseModel):
    strategy: StrategyInfo
    asset: str
    start_date: str
    end_date: str
    curves: list[CurveSeries]
    metrics: dict[str, MetricSummary]
    signal_summary: dict[str, Any]
    trade_summary: TradeSummary
    trade_records: list[TradeRecord]
    daily_position_details: list[DailyPositionDetail]


class BacktestJobCreateResponse(BaseModel):
    job_id: str
    status: str
    progress: float
    message: str


class BacktestJobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: float
    message: str
    result: Optional[BacktestResponse] = None
    error: Optional[str] = None


class BacktestRecordSummary(BaseModel):
    record_id: str
    saved_at: str
    strategy_name: str
    asset: str = ""
    start_date: str
    end_date: str
    initial_capital: float = 0
    metrics: dict[str, MetricSummary] = {}
    strategy_params: dict[str, Any] = {}


class BacktestRecordFull(BaseModel):
    record_id: str
    saved_at: str
    request: BacktestRequest
    result: BacktestResponse


class ComparisonRequest(BaseModel):
    record_ids: list[str] = Field(..., min_length=2, max_length=10)


class ComparisonCurve(BaseModel):
    record_id: str
    label: str
    points: list[CurvePoint]


class ComparisonResponse(BaseModel):
    curves: list[ComparisonCurve]
    date_range: dict[str, str]
