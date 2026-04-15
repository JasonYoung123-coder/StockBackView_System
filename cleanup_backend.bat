@echo off
setlocal EnableExtensions

cd /d "%~dp0"
title StockBackView Backend Cleanup

if not exist "%~dp0cleanup_backend.ps1" (
    echo [ERROR] cleanup_backend.ps1 was not found.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_backend.ps1"
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [WARN] Cleanup finished with warnings. Check the output above.
) else (
    echo.
    echo [INFO] Cleanup complete.
)

pause
exit /b %EXITCODE%
