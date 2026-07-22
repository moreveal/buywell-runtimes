#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
  command -v "$PYTHON" >/dev/null 2>&1 || {
    echo "Python was not found: $PYTHON" >&2
    exit 1
  }
else
  PYTHON=""
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
  [ -n "$PYTHON" ] || {
    echo "Install Python 3.11 or newer, then run this file again." >&2
    exit 1
  }
fi

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 or newer is required." >&2
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  if ! "$PYTHON" -m venv .venv; then
    echo "Could not create the private Python environment. Trying to restore pip..."
    "$PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$PYTHON" -m venv .venv || {
      echo "Python cannot create virtual environments." >&2
      echo "On Ubuntu/Debian run: sudo apt install python3-venv" >&2
      exit 1
    }
  fi
fi

if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  echo "pip is missing. Restoring it with ensurepip..."
  .venv/bin/python -m ensurepip --upgrade || {
    echo "Could not restore pip. Reinstall Python with pip and venv support." >&2
    exit 1
  }
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python configure.py
.venv/bin/python ggsel_runtime.py --config config.json --check-config
.venv/bin/python ggsel_runtime.py --config config.json --check-api
.venv/bin/python ggsel_runtime.py --config config.json --check-buywell

echo "Configuration, GGSel API, and Buywell connection checks passed."

service_started=0
if command -v systemctl >/dev/null 2>&1; then
  printf "Install and start automatic background service? [Y/n]: "
  read -r answer
  case "$answer" in
    n|N|no|NO) ;;
    *)
      if sh ./install-service.sh; then
        service_started=1
      else
        echo "Service setup failed. You can still run the runtime manually." >&2
      fi
      ;;
  esac
fi

[ "$service_started" -eq 0 ] || exit 0
printf "Start the runtime in this window now? [Y/n]: "
read -r answer
case "$answer" in
  n|N|no|NO) echo "Run ./run.sh when ready." ;;
  *) exec .venv/bin/python ggsel_runtime.py --config config.json ;;
esac
