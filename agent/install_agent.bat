@echo off
REM ============================================================
REM  Hermes agent installer  -  run ONCE on a laptop/PC you want
REM  Hermes to control. It copies the agent, sets it to start
REM  automatically at every login (hidden), and starts it now.
REM  Requirement: Python 3 installed (python.org).
REM ============================================================
setlocal

where pythonw >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python 3 is not installed on this PC.
  echo   Install it from https://python.org  ^(tick "Add to PATH"^), then re-run this.
  echo.
  pause
  exit /b 1
)

set "DEST=%LOCALAPPDATA%\HermesAgent"
if not exist "%DEST%" mkdir "%DEST%"
copy /y "%~dp0hermes_agent.py" "%DEST%\hermes_agent.py" >nul

REM The agent refuses every command until it has the brain's token (no public default).
if not exist "%DEST%\hermes_agent_token.txt" (
  echo.
  set /p "HTOKEN=  Paste the brain's HERMES_TOKEN (from bridge\.env) and press Enter: "
)
if not exist "%DEST%\hermes_agent_token.txt" (
  if not defined HTOKEN (
    echo   No token entered - run this again and paste it.
    pause
    exit /b 1
  )
)
if defined HTOKEN (
  > "%DEST%\hermes_agent_token.txt" echo %HTOKEN%
)
REM Stop an older running copy (it may still accept the old public token) so the new one gets the port.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*hermes_agent.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>nul

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
> "%STARTUP%\HermesAgent.vbs" echo Set s=CreateObject("WScript.Shell")
>>"%STARTUP%\HermesAgent.vbs" echo s.Run "pythonw ""%DEST%\hermes_agent.py""", 0, False

start "" pythonw "%DEST%\hermes_agent.py"

echo.
echo   Hermes agent installed and running. It will auto-start at every login.
echo   It listens on port 8799. This PC's address(es):
echo.
ipconfig | findstr /i "IPv4"
echo.
echo   Give the IPv4 above (with :8799) to the brain - add it under
echo   "computers" in the brain's devices.json. Done!
echo.
pause
