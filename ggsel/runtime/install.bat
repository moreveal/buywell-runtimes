@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3.11 or newer is required.
    exit /b 1
  )
  set "PYTHON=python"
)

%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info.minor - 11 >= 0 else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  %PYTHON% -m venv .venv
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" configure.py
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" ggsel_runtime.py --config config.json --check-config
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" ggsel_runtime.py --config config.json --check-api
if errorlevel 1 exit /b 1

set /p START_NOW="Start the runtime now? [Y/n]: "
if /I "%START_NOW%"=="n" (
  echo Run run.bat when ready.
  exit /b 0
)
if /I "%START_NOW%"=="no" (
  echo Run run.bat when ready.
  exit /b 0
)
call run.bat
