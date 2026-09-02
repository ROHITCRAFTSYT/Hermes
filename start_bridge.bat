@echo off
REM -- Starts the Hermes brain. Double-click this file. --
setlocal
cd /d "%~dp0bridge"

set "PYTHON_EXE="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
  echo.
  echo   Python 3.11+ was not found. Install Python from https://python.org,
  echo   tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

set "VENV_PY=.venv\Scripts\python.exe"
set "REBUILD_VENV="
if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys" >nul 2>nul
  if errorlevel 1 set "REBUILD_VENV=1"
) else (
  set "REBUILD_VENV=1"
)

if defined REBUILD_VENV (
  echo Creating or repairing the Python virtual environment...
  if exist ".venv" (
    ren ".venv" ".venv.broken-%RANDOM%"
    if errorlevel 1 (
      echo.
      echo   Could not repair .venv because it is in use.
      echo   Close any running Hermes bridge windows, then run this again.
      echo.
      pause
      exit /b 1
    )
  )
  "%PYTHON_EXE%" -m venv .venv
  if errorlevel 1 (
    echo.
    echo   Failed to create the virtual environment.
    echo.
    pause
    exit /b 1
  )
  "%VENV_PY%" -m pip install --upgrade pip
  "%VENV_PY%" -m pip install -r requirements.txt
)
if not exist ".env" (
  echo.
  echo  !!  No .env file found. Copy .env.example to .env and add your Groq key.
  echo.
)
echo Starting Hermes bridge...
.venv\Scripts\python hermes_bridge.py
pause
