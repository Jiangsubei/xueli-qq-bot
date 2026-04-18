@echo off
setlocal
pushd "%~dp0"
chcp 65001 >nul

echo.
echo ========================================
echo        QQ AI Launcher
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found. Please install Python 3.8+ first.
    pause
    exit /b 1
)

if not exist "venv" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo [INFO] Activating virtual environment...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

echo [INFO] Installing dependencies...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo [WARN] .env was not found.
    echo [INFO] Creating .env from .env.example...
    copy /Y .env.example .env >nul
    echo.
    echo ========================================
    echo Edit .env and run start.bat again.
    echo ========================================
    echo.
    notepad .env
    pause
    exit /b 1
)

echo.
echo ========================================
echo Starting bot and WebUI...
echo Press Ctrl+C to stop.
echo ========================================
echo.

python main.py
set "APP_EXIT=%ERRORLEVEL%"

if exist "venv\Scripts\deactivate.bat" (
    call venv\Scripts\deactivate.bat
)

echo.
echo Service stopped.
pause
exit /b %APP_EXIT%
