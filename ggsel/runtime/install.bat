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

%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  %PYTHON% -m venv .venv
  if errorlevel 1 (
    echo Could not create the private Python environment. Trying to restore pip...
    %PYTHON% -m ensurepip --upgrade >nul 2>nul
    %PYTHON% -m venv .venv
    if errorlevel 1 (
      echo Python cannot create virtual environments. Reinstall Python with pip enabled.
      exit /b 1
    )
  )
)

".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip is missing. Restoring it with ensurepip...
  ".venv\Scripts\python.exe" -m ensurepip --upgrade
  if errorlevel 1 (
    echo Could not restore pip. Reinstall Python with pip enabled.
    exit /b 1
  )
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
".venv\Scripts\python.exe" ggsel_runtime.py --config config.json --check-buywell
if errorlevel 1 exit /b 1

echo Configuration, GGSel API, and Buywell connection checks passed.

set /p INSTALL_SERVICE="Install and start automatic background task? [Y/n]: "
if /I not "%INSTALL_SERVICE%"=="n" if /I not "%INSTALL_SERVICE%"=="no" (
  call install-service.bat
  if not errorlevel 1 exit /b 0
  echo Background task setup failed. You can still run the runtime manually.
)

set /p START_NOW="Start the runtime in this window now? [Y/n]: "
if /I "%START_NOW%"=="n" (
  echo Run run.bat when ready.
  exit /b 0
)
if /I "%START_NOW%"=="no" (
  echo Run run.bat when ready.
  exit /b 0
)
call run.bat
