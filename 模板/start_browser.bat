@echo off
cd /d "%~dp0"
mode con cols=100 lines=42 >nul 2>nul
title <客户名> data browsers

python 启动取数浏览器.py %*

echo.
pause