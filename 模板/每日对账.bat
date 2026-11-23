@echo off
cd /d "%~dp0"
mode con cols=112 lines=45 >nul 2>nul
title <客户名> data reconcile
python 每日对账.py %*
echo.
pause