@echo off
setlocal

cd /d "%~dp0"

set "PY_CMD="

if exist ".venv\Scripts\python.exe" set "PY_CMD=.venv\Scripts\python.exe"
if not defined PY_CMD if exist ".venv\Scripts\pythonw.exe" set "PY_CMD=.venv\Scripts\pythonw.exe"
if not defined PY_CMD where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD where python >nul 2>nul && set "PY_CMD=python"

if not defined PY_CMD (
    echo Could not find Python.
    echo Install Python 3 or create .venv in this folder.
    pause
    exit /b 1
)

REM Install dependencies if any are missing.
if exist "requirements.txt" (
    %PY_CMD% -c "import requests, dotenv" >nul 2>nul
    if errorlevel 1 (
        echo Installing dependencies from requirements.txt ...
        %PY_CMD% -m pip install -r requirements.txt
        if errorlevel 1 (
            echo.
            echo Failed to install dependencies from requirements.txt.
            pause
            exit /b 1
        )
    )
)

%PY_CMD% app.py
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo FedFish exited with error code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
