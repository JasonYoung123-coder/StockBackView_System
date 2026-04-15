from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class TradingConnectRequest(BaseModel):
    xtquant_path: str = Field("", description="xtquant 所在 site-packages 路径，留空则读 config")
    userdata_path: str = Field("", description="QMT userdata 路径，留空则读 config")
    account_id: str = Field("", description="资金账号，留空则读 config")
    account_type: str = Field("STOCK", description="账户类型: STOCK / CREDIT / FUTURE")


class AccountInfo(BaseModel):
    total_asset: float = 0.0
    available_cash: float = 0.0
    market_value: float = 0.0
    frozen_cash: float = 0.0


class PositionItem(BaseModel):
    ts_code: str
    name: str = ""
    volume: int = 0
    available_volume: int = 0
    cost_price: float = 0.0
    market_value: float = 0.0
    profit: float = 0.0
    profit_rate: float = 0.0


class OrderItem(BaseModel):
    ts_code: str
    name: str = ""
    direction: Literal["buy", "sell"]
    price: float = 0.0
    volume: int = 0
    amount: float = 0.0
    price_type: str = "最新价"
    status: str = "pending"
    remark: str = ""


class TradingRunRequest(BaseModel):
    strategy_name: str = Field(..., description="策略名称")
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    price_type: Literal["latest", "limit"] = Field("latest", description="下单价格类型")
    lookback_days: int = Field(250, ge=30, description="策略回看天数")


class TradingJobStatus(BaseModel):
    job_id: str
    status: str = "pending"
    progress: float = 0.0
    message: str = ""
    account_info: Optional[AccountInfo] = None
    current_positions: list[PositionItem] = Field(default_factory=list)
    target_weights: dict[str, float] = Field(default_factory=dict)
    orders: list[OrderItem] = Field(default_factory=list)
    execution_log: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class TradingAccountResponse(BaseModel):
    connected: bool
    account_info: Optional[AccountInfo] = None
    positions: list[PositionItem] = Field(default_factory=list)


class SchedulerStartRequest(BaseModel):
    strategy_name: str = Field(..., description="策略名称")
    fund_ratio: float = Field(1.0, ge=0.2, le=1.0, description="资金比例 0.2~1.0")
    buy_existing: bool = Field(False, description="是否买入策略已持仓的股票")
    allow_sell: bool = Field(True, description="是否自动卖出持仓")
    price_type: Literal["latest", "limit"] = Field("latest", description="下单价格类型")
    lookback_days: int = Field(250, ge=30, description="策略回看天数")
    live_start_date: str = Field("", description="实盘起始日(YYYY-MM-DD)，留空则默认当天")


class PendingBuyItem(BaseModel):
    ts_code: str
    name: str = ""


class PendingSellItem(BaseModel):
    ts_code: str
    name: str = ""
    reason: str = ""


class SchedulerStatusResponse(BaseModel):
    running: bool = False
    strategy_name: str = ""
    fund_ratio: float = 1.0
    buy_existing: bool = False
    allow_sell: bool = True
    last_execution: str = ""
    next_execution: str = ""
    today_executed: bool = False
    today_orders: list[OrderItem] = Field(default_factory=list)
    execution_log: list[str] = Field(default_factory=list)
    account_info: Optional[AccountInfo] = None
    positions: list[PositionItem] = Field(default_factory=list)
    pending_sell_signals: list[PendingSellItem] = Field(default_factory=list)
    pending_buy_signals: list[PendingBuyItem] = Field(default_factory=list)
    error: Optional[str] = None
