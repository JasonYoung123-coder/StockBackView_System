@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

echo ═══════════════════════════════════════════════════
echo   StockBackView System 1.0.1 - 一键部署脚本
echo ═══════════════════════════════════════════════════
echo.

:: ─── 定位项目根目录（bat 所在目录） ───
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
echo [信息] 项目目录: %PROJECT_DIR%
echo.

:: ═══════════════════════════════════════════════════
::  第一步：检查 Python 是否已安装
:: ═══════════════════════════════════════════════════
echo [1/5] 检查 Python 环境...

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9 或更高版本。
    echo        下载地址: https://www.python.org/downloads/
    echo        安装时请勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
echo        已检测到: %PY_VER%

:: 检查版本 >= 3.9
for /f "tokens=2 delims= " %%a in ("%PY_VER%") do set "VER_NUM=%%a"
for /f "tokens=1,2 delims=." %%a in ("%VER_NUM%") do (
    set "MAJOR=%%a"
    set "MINOR=%%b"
)
if %MAJOR% lss 3 (
    echo [错误] Python 版本过低，需要 3.9+，当前: %VER_NUM%
    pause
    exit /b 1
)
if %MAJOR% equ 3 if %MINOR% lss 9 (
    echo [错误] Python 版本过低，需要 3.9+，当前: %VER_NUM%
    pause
    exit /b 1
)
echo        版本检查通过
echo.

:: ═══════════════════════════════════════════════════
::  第二步：创建虚拟环境
:: ═══════════════════════════════════════════════════
echo [2/5] 创建 Python 虚拟环境...

set "VENV_OK=0"
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe --version >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "VENV_OK=1"
        echo        虚拟环境已存在且可用，跳过创建
    ) else (
        echo [警告] 虚拟环境已损坏（解释器不可用），正在重建...
        rmdir /s /q .venv 2>nul
    )
)
if "!VENV_OK!"=="0" (
    echo        正在创建 .venv ...
    python -m venv .venv
    if !ERRORLEVEL! neq 0 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
    echo        虚拟环境创建成功
)
echo.

:: ═══════════════════════════════════════════════════
::  第三步：激活虚拟环境并升级 pip
:: ═══════════════════════════════════════════════════
echo [3/5] 激活虚拟环境并升级 pip...

call .venv\Scripts\activate.bat

python -m pip install --upgrade pip --quiet
if %ERRORLEVEL% neq 0 (
    echo [警告] pip 升级失败，继续安装...
)
echo        pip 已就绪
echo.

:: ═══════════════════════════════════════════════════
::  第四步：安装 Python 依赖包
:: ═══════════════════════════════════════════════════
echo [4/5] 安装项目依赖（requirements.txt）...
echo        这可能需要几分钟，请耐心等待...
echo.

pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo [错误] 部分依赖安装失败，请检查上方错误信息
    echo        常见原因：网络问题、pip 源不可用
    echo        可尝试使用国内镜像：
    echo        pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)
echo.
echo        所有依赖安装成功
echo.

:: ═══════════════════════════════════════════════════
::  第五步：检查配置文件
:: ═══════════════════════════════════════════════════
echo [5/5] 检查配置文件...

if exist "config\config.toml" (
    echo        config\config.toml 已存在
) else (
    echo [警告] config\config.toml 不存在
    echo        请参照 config 目录下的示例文件创建配置
)
echo.

:: ═══════════════════════════════════════════════════
::  创建必要的数据目录
:: ═══════════════════════════════════════════════════
if not exist "data\cache\backtest_cache" mkdir "data\cache\backtest_cache"
if not exist "data\cache\chip_cache" mkdir "data\cache\chip_cache"

:: ═══════════════════════════════════════════════════
::  部署完成，输出后续操作指引
:: ═══════════════════════════════════════════════════
echo ═══════════════════════════════════════════════════
echo   部署完成！
echo ═══════════════════════════════════════════════════
echo.
echo   后续操作：
echo.
echo   1. 编辑配置文件 config\config.toml：
echo      - 填写 Tushare Token（必填）
echo      - 如需实盘交易，填写 QMT 终端路径和资金账号
echo.
echo   2. 如需实盘交易（QMT）：
echo      - 安装 QMT 交易终端
echo      - 将 config.toml 中 [qmt] 的路径指向 QMT 安装目录
echo.
echo   3. 启动系统：
echo      .venv\Scripts\python.exe run_backend.py
echo.
echo   4. 打开浏览器访问：
echo      http://127.0.0.1:8000
echo.
echo   如需外网访问，启动前设置环境变量：
echo      set APP_HOST=0.0.0.0
echo      .venv\Scripts\python.exe run_backend.py
echo.
echo ═══════════════════════════════════════════════════
echo.
pause
