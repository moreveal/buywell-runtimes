#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec .venv/bin/python runtime/ggsel_runtime.py --config config.json
