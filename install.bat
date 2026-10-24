@echo off
REM One-time setup for Windows: double-click this file.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>nul || (echo Python 3.10 or newer is needed: https://www.python.org/downloads/  - tick "Add python.exe to PATH" when installing, then run this again. & pause & exit /b 1)
echo Installing (about a minute)...
%PY% -m venv .venv
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python -m signals setup
echo.
echo Done. Double-click start.bat to run it (leave the window open).
pause
