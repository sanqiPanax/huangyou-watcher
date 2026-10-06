@echo off
rem huangyou-watcher: pull all sources, update table, open report
cd /d "%~dp0"
set PYTHONUTF8=1
python pull.py
echo.
echo === report: %~dp0report.html ===
if exist "%~dp0report.html" start "" "%~dp0report.html"
pause
