@echo off
setlocal
title Ledger Dashboard
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing project environment. Run: uv venv .venv
  echo Then: uv pip install --python .venv/Scripts/python.exe -r requirements.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m scripts.serve_dashboard --open-browser %*
if errorlevel 1 (
  echo Dashboard did not start. See the error above.
  pause
  exit /b 1
)
endlocal
