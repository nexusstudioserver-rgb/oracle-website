@echo off
title Oracle - local engine
cd /d "%~dp0"

echo ============================================================
echo  Oracle - starting the local decompiler engine
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found on this PC.
  echo.
  echo Install it from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" during the install.
  echo.
  pause
  exit /b 1
)

echo Opening Oracle.html in your browser...
start "" "%~dp0Oracle.html"

echo Engine is running. Leave this window OPEN while you use the tool.
echo Type ANY key on the login screen - no key is ever checked.
echo Press Ctrl+C or close this window to stop.
echo.

python "%~dp0oracle_server.py" --port 8787
pause
