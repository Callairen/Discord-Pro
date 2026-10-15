@echo off
rem Builds dist\DiscordPro\DiscordPro.exe with PyInstaller.
cd /d "%~dp0\..\.."
call .venv\Scripts\activate.bat || (echo Run install.bat first & exit /b 1)
python -m pip install -r requirements-dev.txt || exit /b 1
pyinstaller packaging\DiscordPro.spec --noconfirm || exit /b 1
echo Built: dist\DiscordPro\DiscordPro.exe
