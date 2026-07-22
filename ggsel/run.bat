@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" runtime\ggsel_runtime.py --config config.json
