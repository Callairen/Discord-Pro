@echo off
rem Starts the Discord-Pro desktop app without a console window.
cd /d "%~dp0\..\.."
if not exist .venv\Scripts\pythonw.exe (
  echo Run scripts\windows\install.bat first.
  pause
  exit /b 1
)
start "" .venv\Scripts\pythonw.exe -m discord_pro
