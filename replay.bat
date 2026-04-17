@echo off
chcp 65001 >nul 2>&1

REM ══════════════════════════════════════════════════
REM  实盘回放模拟器 快速启动脚本
REM  修改下方参数后双击运行即可
REM ══════════════════════════════════════════════════

REM ── 回放日期区间 ──
set START_DATE=2025-04-01
set END_DATE=2025-04-15

REM ── 策略名称 ──
set STRATEGY=Jason_selector_strategy2.0.3

REM ── 初始资金 ──
set CAPITAL=100000

REM ── 资金比例 (0.2~1.0) ──
set FUND_RATIO=1.0

REM ── 回看天数 ──
set LOOKBACK=250

REM ── 实盘起始日 (留空则等于 START_DATE) ──
set LIVE_START=

REM ── 是否输出每日详细信号 (加 -v 开启) ──
set VERBOSE=-v

REM ══════════════════════════════════════════════════
REM  以下无需修改
REM ══════════════════════════════════════════════════

if "%LIVE_START%"=="" (
    set LIVE_START_ARG=
) else (
    set LIVE_START_ARG=--live-start %LIVE_START%
)

.venv\Scripts\python.exe run_replay.py ^
    --strategy %STRATEGY% ^
    --start %START_DATE% ^
    --end %END_DATE% ^
    --capital %CAPITAL% ^
    --fund-ratio %FUND_RATIO% ^
    --lookback %LOOKBACK% ^
    %LIVE_START_ARG% ^
    %VERBOSE%

echo.
pause
