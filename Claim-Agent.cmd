@echo off
setlocal DisableDelayedExpansion
title Claim-Agent
chcp 65001 >nul
set PYTHONUTF8=1
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\launch_web.py" %*
) else (
  python "scripts\launch_web.py" %*
)
if errorlevel 1 (
  echo.
  echo Claim-Agent could not start. Please check the message above.
  pause
)
popd
endlocal
