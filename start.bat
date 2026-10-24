@echo off
REM Runs everything automatically every weekday. Leave this window open.
cd /d "%~dp0"
if "%~1"=="" (.venv\Scripts\python -m signals start) else (.venv\Scripts\python -m signals %*)
pause
