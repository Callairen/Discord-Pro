@echo off
rem Creates a virtual environment in .venv and installs Discord-Pro's dependencies.
cd /d "%~dp0\..\.."
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || goto :error
echo.
echo Installed. Start the app with scripts\windows\run.bat
pause
exit /b 0
:error
echo Installation failed. Make sure Python 3.10+ is installed and on PATH.
pause
exit /b 1
