from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.backtest import router as backtest_router
from app.api.trading import router as trading_router
from app.core.config import get_settings

logger = logging.getLogger(__name__)

app = FastAPI(title="劲帆交易回测系统", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    logger.error(
        "请求验证失败 %s %s → %s",
        request.method,
        request.url.path,
        errors,
    )
    return JSONResponse(status_code=422, content={"detail": errors})

settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(backtest_router)
app.include_router(trading_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        context={
            "config_ready": bool(settings.tushare_token),
            "qmt_ready": bool(settings.qmt_account_id),
            "strategies_dir": str(settings.strategies_dir),
            "config_path": str(settings.config_path),
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
