#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
command -v gcc >/dev/null || { echo "gcc is required"; exit 1; }
command -v npm >/dev/null || { echo "npm is required"; exit 1; }
make -C "$ROOT/backend"
(cd "$ROOT/frontend" && npm install)
(cd "$ROOT/frontend" && npm run build)
cd "$ROOT"
exec "$ROOT/backend/syswatch"
