@echo off
rem huangyou-watcher: add a game by pasting an X/Steam/DLsite link
cd /d "%~dp0"
set PYTHONUTF8=1
set /p LINK=Paste X/Steam/DLsite link then press Enter: 
if "%LINK%"=="" (
  echo No input, cancelled.
  pause
  exit /b 1
)
python add_game.py "%LINK%"
echo.
pause
