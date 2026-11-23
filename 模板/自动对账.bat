@echo off
cd /d "%~dp0"
mode con cols=110 lines=52 >nul 2>nul
title <客户名> daily reconcile - auto

rem ★ 计划任务用的是绝对路径，这个 bat 只是手动调试用。
rem   换了 Python 版本/位置，改下面这一行（注意反斜杠）。
set "PY=<Python解释器>"
if not exist "%PY%" (
    echo [ERROR] Python not found:
    echo     %PY%
    echo Edit this bat and set PY to your real python.exe
    pause
    exit /b 9
)

"%PY%" 自动对账.py %*
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
echo   Exit code: %RC%
echo     0=all pass   1=fetch fail   2=missing deps
echo     3=alarm      4=incomplete   5=write fail   9=env not ready
echo ============================================================
pause
exit /b %RC%