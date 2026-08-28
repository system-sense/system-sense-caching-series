#!/usr/bin/env bash
#
# Runs the Episode 1 demo end to end and records what actually happened.
#
# Everything the episode claims on screen comes out of this script. If a number
# changes when you run it on your machine, the number was real.
#
#   ./scripts/capture-demo.sh
#
# Writes:  capture/*.log  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
N=${N:-30}
mkdir -p "$OUT"

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
dc()   { docker compose "$@"; }

wait_healthy() {
  printf 'waiting for app '
  for _ in $(seq 1 90); do
    if curl -fsS localhost:8000/health >/dev/null 2>&1; then echo ' ready'; return 0; fi
    printf '.'; sleep 1
  done
  echo ' TIMED OUT'; dc logs app | tail -40; return 1
}

# Fire N requests, print one line each, and emit the elapsed times to stdout fd 3.
bench() {
  local label=$1 file=$2
  : > "$file"
  for i in $(seq 1 "$N"); do
    curl -fsS -D - -o /dev/null "localhost:8000/api/users/42" \
      | awk -v i="$i" '
          tolower($1) ~ /^x-cache:/        { c=$2 }
          tolower($1) ~ /^x-elapsed-ms:/   { e=$2 }
          END { printf "req %-3d  %-5s %8.2f ms\n", i, c, e }
        ' | tr -d '\r' | tee -a "$file"
  done
}

log "1/6  Tearing down any previous run"
dc down -v --remove-orphans >/dev/null 2>&1 || true

log "2/6  Starting the stack with the cache DISABLED"
CACHE_ENABLED=false dc up --build -d 2>&1 | tee "$OUT/01-compose-up.log"
wait_healthy

log "3/6  What Postgres actually does for a profile read"
# TIMING OFF on purpose. Per-node instrumentation roughly doubles the reported
# Execution Time on a plan this shape, which would contradict the request
# latencies measured below. We want the plan's shape and an honest total.
dc exec -T postgres psql -U sysense -d sysense -c '\timing on' \
  -c "$(python3 - <<'PY'
import re, pathlib
sql = pathlib.Path("app/queries.py").read_text()
stats = re.search(r'PROFILE_STATS = """(.*?)"""', sql, re.S).group(1)
print("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) " + stats.replace("$1", "42"))
PY
)" 2>&1 | tee "$OUT/02-explain-analyze.log"

log "4/6  Hitting /api/users/42 with the cache OFF  (${N}x)"
bench "cold" "$OUT/03-cache-off.log"

log "5/6  Flipping the knob: CACHE_ENABLED=true"
CACHE_ENABLED=true dc up -d --no-deps app 2>&1 | tee "$OUT/04-flip-knob.log"
wait_healthy
bench "warm" "$OUT/05-cache-on.log"

log "6/6  What the cache costs in memory"
{
  dc exec -T redis redis-cli INFO memory | grep -E 'used_memory_human|used_memory:' | tr -d '\r'
  dc exec -T redis redis-cli MEMORY USAGE user:42 | tr -d '\r' | sed 's/^/user:42 bytes: /'
  dc exec -T redis redis-cli DBSIZE | tr -d '\r' | sed 's/^/keys: /'
} 2>&1 | tee "$OUT/06-redis-memory.log"

log "Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
