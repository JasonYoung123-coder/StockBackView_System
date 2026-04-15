@echo off
setlocal EnableExtensions EnableDelayedExpansion

chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0"
title StockBackView Launcher

set "PY_BOOTSTRAP=py -3"
%PY_BOOTSTRAP% --version >nul 2>nul
if errorlevel 1 set "PY_BOOTSTRAP=python"

%PY_BOOTSTRAP% --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未检测到 Python，请先安装 Python 3 并加入 PATH。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [INFO] 正在创建虚拟环境 .venv ...
    %PY_BOOTSTRAP% -m venv ".venv"
    if errorlevel 1 (
        echo [ERROR] 虚拟环境创建失败。
        pause
        exit /b 1
    )
)

set "VENV_PY=%CD%\.venv\Scripts\python.exe"

echo [INFO] 正在检查依赖...
"%VENV_PY%" -c "import fastapi, uvicorn, jinja2, pandas, tushare, multipart" >nul 2>nul
if errorlevel 1 (
    echo [INFO] 正在安装后端依赖，首次运行可能需要几分钟...
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 (
        echo [ERROR] pip 升级失败。
        pause
        exit /b 1
    )
    "%VENV_PY%" -m pip install fastapi "uvicorn[standard]" jinja2 pandas tushare python-multipart tomli
    if errorlevel 1 (
        echo [ERROR] 依赖安装失败。
        pause
        exit /b 1
    )
)

if not exist "config\config.toml" (
    echo [WARN] 未找到 config\config.toml，正在创建空模板...
    > "config\config.toml" echo [tushare]
    >> "config\config.toml" echo token = ""
    >> "config\config.toml" echo.
    >> "config\config.toml" echo [data]
    >> "config\config.toml" echo dir = "data"
    >> "config\config.toml" echo cache_dir = "cache"
    >> "config\config.toml" echo.
    >> "config\config.toml" echo [strategy]
    >> "config\config.toml" echo dir = "strategies"
    >> "config\config.toml" echo.
    >> "config\config.toml" echo [backtest]
    >> "config\config.toml" echo default_commission_rate = 0.0003
    >> "config\config.toml" echo default_stamp_duty_rate = 0.001
    >> "config\config.toml" echo.
    >> "config\config.toml" echo [benchmarks]
    >> "config\config.toml" echo "上证指数" = "000001.SH"
    >> "config\config.toml" echo "沪深300" = "000300.SH"
    >> "config\config.toml" echo "中证1000" = "000852.SH"
)

if exist "cleanup_backend.ps1" (
    echo [INFO] 启动前先清理旧后端...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_backend.ps1"
    if errorlevel 1 (
        echo [ERROR] 旧后端清理失败，请先手动检查端口占用后重试。
        pause
        exit /b 1
    )
) else (
    echo [WARN] 未找到 cleanup_backend.ps1，跳过旧后端清理。
)

set "APP_PORT=8000"
echo [INFO] 固定使用端口 %APP_PORT%
powershell -NoProfile -Command "$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, %APP_PORT%); try { $listener.Start(); $listener.Stop(); exit 0 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 端口 %APP_PORT% 仍被占用，请关闭占用该端口的程序后重试。
    pause
    exit /b 1
)

set "APP_URL=http://127.0.0.1:%APP_PORT%/"
set "APP_HOST=127.0.0.1"
set "APP_RELOAD=1"
set "APP_VENV_ROOT=%CD%\.venv"

start "StockBackView Backend" cmd /k ""%VENV_PY%" -X utf8 run_backend.py"

echo [INFO] 正在等待后端启动...
powershell -NoProfile -Command "$url='http://127.0.0.1:%APP_PORT%/health'; for($i=0; $i -lt 60; $i++){ try { $resp = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2; if($resp.StatusCode -eq 200){ exit 0 } } catch {} Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
    echo [WARN] 后端启动超时，请查看新打开的后端窗口日志。
    echo [INFO] 你也可以稍后手动打开：%APP_URL%
    pause
    exit /b 1
)

echo [INFO] 后端已启动，正在打开浏览器...
start "" "%APP_URL%"
echo [INFO] 回测网页已打开：%APP_URL%
exit /b 0
