#!/usr/bin/env sh
# Linux/macOS wrapper: ./scripts/demo.sh <verb> [options]   (see scripts/demo.py)
set -eu
cd "$(dirname "$0")/.."
exec uv run --quiet scripts/demo.py "$@"
