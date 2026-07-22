#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 or newer is required." >&2
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python configure.py
.venv/bin/python ggsel_runtime.py --config config.json --check-config
.venv/bin/python ggsel_runtime.py --config config.json --check-api

printf "Start the runtime now? [Y/n]: "
read -r answer
case "$answer" in
  n|N|no|NO) echo "Run ./run.sh when ready." ;;
  *) exec .venv/bin/python ggsel_runtime.py --config config.json ;;
esac
