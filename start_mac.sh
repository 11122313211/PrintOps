#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PRINTOPS_PYTHON="${PRINTOPS_PYTHON:-python3}"

echo "PrintOps is starting at http://localhost:${PRINTOPS_PORT:-4174}"
exec "$PRINTOPS_PYTHON" server.py
