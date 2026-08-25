@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYTHONW=%SCRIPT_DIR%..\.venv\Scripts\pythonw.exe"
set "RUN_PY=%SCRIPT_DIR%run.py"

if not exist "%PYTHONW%" (
    echo Virtual environment not found: %PYTHONW%
    echo.
    echo Set it up first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

start "" "%PYTHONW%" "%RUN_PY%"
