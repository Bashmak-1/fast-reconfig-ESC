@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYTHON=%SCRIPT_DIR%..\.venv\Scripts\python.exe"
set "BUILD_PY=%SCRIPT_DIR%build_exe.py"

if not exist "%PYTHON%" (
    echo Virtual environment not found: %PYTHON%
    echo.
    echo Set it up first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

"%PYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found in the virtual environment - installing it...
    "%PYTHON%" -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo Failed to install PyInstaller. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

echo.
echo Building EscHotline.exe ...
echo.
"%PYTHON%" "%BUILD_PY%"
if errorlevel 1 (
    echo.
    echo Build failed - see the output above.
    pause
    exit /b 1
)

echo.
echo Done: %SCRIPT_DIR%..\dist\EscHotline.exe
pause
