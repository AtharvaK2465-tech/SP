#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT=${PORT:-8080}
make -C "$ROOT/backend" clean >/dev/null
make -C "$ROOT/backend" PORT="$PORT" >/dev/null
"$ROOT/backend/syswatch" >/tmp/syswatch-test.log 2>&1 & SW=$!
BURN=""
cleanup(){ [ -z "$BURN" ] || kill "$BURN" 2>/dev/null || true; kill "$SW" 2>/dev/null || true; }
trap cleanup EXIT
sleep 1
if ! curl -fsS "http://127.0.0.1:$PORT/api/stats" >/tmp/syswatch-stats.json; then echo "SysWatch did not start on port $PORT"; exit 1; fi
if command -v yes >/dev/null; then yes >/dev/null & BURN=$!; fi
sleep 3
curl -fsS "http://127.0.0.1:$PORT/api/stats" >/tmp/syswatch-stats.json
python3 - <<'PY'
import json
x=json.load(open('/tmp/syswatch-stats.json'))
assert x['processCount'] > 0
assert isinstance(x['memory']['total'], int)
assert any(p['name'] == 'yes' for p in x['processes'])
print('SysWatch process count:',x['processCount'])
print('SysWatch memory used %:',round(x['memory']['usedPct'],2))
print('Top CPU:',sorted([(p['cpu'],p['pid'],p['name']) for p in x['processes']],reverse=True)[:3])
PY
printf '
free -b:
'; free -b
printf '
ps process count:
'; ps -e --no-headers | wc -l
printf '
SysWatch process count:
'; python3 -c 'import json;print(json.load(open("/tmp/syswatch-stats.json"))["processCount"])'
