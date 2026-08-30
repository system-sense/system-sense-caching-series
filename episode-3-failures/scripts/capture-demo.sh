#!/usr/bin/env bash
#
# Runs the Episode 3 demo end to end and records what actually happened.
#
# Three failures, two fixes, one deliberately left broken. Everything the
# episode claims on screen comes out of this script. If a number changes when
# you run it on your machine, the number was real.
#
#   ./scripts/capture-demo.sh
#
# Writes:  capture/*.log, capture/*.json  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
mkdir -p "$OUT"

# ── The dials. Defaults are what the episode was captured with. ──────────────
KEYS=${KEYS:-5000}            # keys the batch job warms
WARM_TTL=${WARM_TTL:-25}      # seconds until the warmed keys expire
JITTER=${JITTER:-30}          # rand(0, JITTER) added to each TTL, in the fix
AV_RPS=${AV_RPS:-600}         # ordinary traffic: nothing to Redis, ~2x what Postgres can serve
AV_DURATION=${AV_DURATION:-60s}
PEN_VUS=${PEN_VUS:-40}
PEN_DURATION=${PEN_DURATION:-15s}
HOT_RPS=${HOT_RPS:-2000}      # one key, and Redis does not even notice
HOT_DURATION=${HOT_DURATION:-25s}
HOT_EXPIRE_AT=${HOT_EXPIRE_AT:-10}   # seconds into the run that the hot key dies

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
dc()   { docker compose "$@"; }
api()  { curl -fsS "$@"; }
post() { curl -fsS -X POST -H 'content-type: application/json' "$@"; }
py()   { python3 -c "$1"; }

wait_healthy() {
  printf 'waiting for app '
  for _ in $(seq 1 120); do
    if curl -fsS localhost:8000/health >/dev/null 2>&1; then echo ' ready'; return 0; fi
    printf '.'; sleep 1
  done
  echo ' TIMED OUT'; dc logs app | tail -40; return 1
}

# Run a k6 script, tee the human output, keep the machine-readable line.
k6run() {                     # k6run <script> <out-name> [ENV=V ...]
  local script=$1 name=$2; shift 2
  local envs=(-e "BASE=http://app:8000")
  for kv in "$@"; do envs+=(-e "$kv"); done
  dc run --rm "${envs[@]}" load-test run "/scripts/$script" 2>&1 \
    | tee "$OUT/$name.log" \
    | grep '^K6_SUMMARY ' | sed 's/^K6_SUMMARY //' > "$OUT/$name.json" || true
  if [ ! -s "$OUT/$name.json" ]; then
    echo "  !! no k6 summary from $script -- see $OUT/$name.log" >&2
  fi
}

# Freeze both sides of the story: what the app counted, and what Postgres did.
snap() {                      # snap <phase>
  local phase=$1
  api localhost:8000/metrics        > "$OUT/m-$phase.json"
  api localhost:8000/metrics/series > "$OUT/s-$phase.json"
  api localhost:8000/api/pg/stats   > "$OUT/pg-$phase.json"
}

arm() {                       # zero every counter, both sides
  post localhost:8000/metrics/reset >/dev/null
  post localhost:8000/api/pg/reset  >/dev/null
}

defense() {                   # defense <none|null_cache|bloom> [jitter_seconds]
  post localhost:8000/api/defense \
    -d "{\"defense\":\"$1\",\"jitter_seconds\":${2:-0}}" | tr -d '\r'; echo
}

# Postgres CPU, sampled while an attack runs. Backgrounded; stopped with `cpu_stop`.
cpu_start() {                 # cpu_start <phase>
  local cid; cid=$(dc ps -q postgres)
  : > "$OUT/cpu-$1.log"
  ( while :; do
      docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' "$cid" 2>/dev/null \
        | sed "s/^/$(date +%s) /" >> "$OUT/cpu-$1.log" || true
    done ) &
  CPU_PID=$!
}
cpu_stop() { kill "${CPU_PID:-0}" 2>/dev/null || true; wait "${CPU_PID:-0}" 2>/dev/null || true; }

# ═══════════════════════════════════════════════════════════════════════════

log "1/10  Tearing down any previous run"
dc down -v --remove-orphans >/dev/null 2>&1 || true

log "2/10  Starting the stack -- both defenses OFF, which is the bug"
PENETRATION_DEFENSE=none TTL_JITTER_SECONDS=0 dc up --build -d 2>&1 | tee "$OUT/01-compose-up.log"
wait_healthy
api localhost:8000/health > "$OUT/health.json"
py "import json;d=json.load(open('$OUT/health.json'));b=d['bloom'];print(f\"  bloom: {b['items']} ids in {b['size_kib']} KiB, k={b['hashes']}, built in {d['bloom_build_ms']} ms\")"

log "3/10  Episodes 1 and 2 still work  (the callback, re-measured here)"
{
  for i in $(seq 1 20); do
    curl -fsS -D - -o /dev/null localhost:8000/api/users/42 \
      | awk -v i="$i" '
          tolower($1) ~ /^x-cache:/      { c=$2 }
          tolower($1) ~ /^x-elapsed-ms:/ { e=$2 }
          END { printf "req %-3d  %-5s %8.2f ms\n", i, c, e }' | tr -d '\r'
  done
  curl -fsS -D - -o /dev/null localhost:8000/api/users/42/uncached \
    | awk 'tolower($1) ~ /^x-elapsed-ms:/ { printf "uncached      %8.2f ms\n", $2 }' | tr -d '\r'
} 2>&1 | tee "$OUT/02-baseline-reads.log"

log "4/10  How much can Postgres actually take?  (the ceiling everything else is measured against)"
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
arm
k6run ceiling.js 03-db-ceiling "KEYS=$KEYS"
snap ceiling
py "import json;d=json.load(open('$OUT/03-db-ceiling.json'));print(f\"  Postgres serves {d['rps']} profile aggregates/sec, median {d['latency_ms']['med']} ms\")"

# ...and the same question of the other layer, for the gap between them.
post localhost:8000/api/cache/warm -d "{\"count\":$KEYS,\"ttl\":300,\"jitter\":0}" >/dev/null
arm
k6run ceiling.js 03b-cache-ceiling "MODE=cache" "KEYS=$KEYS" "RPS=2500" "DURATION=10s"
py "import json;d=json.load(open('$OUT/03b-cache-ceiling.json'));print(f\"  Redis serves {d['rps']} req/sec at {d['latency_ms']['med']} ms median, {d['failed_pct']}% failed\")"

# ── Failure 1: Penetration ───────────────────────────────────────────────────

log "5/10  PENETRATION, undefended: ids that were never issued"
defense none 0
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
arm
k6run penetration.js 04-penetration-none "MODE=random" "VUS=$PEN_VUS" "DURATION=$PEN_DURATION" "LABEL=penetration-undefended"
snap penetration-none
py "import json;d=json.load(open('$OUT/m-penetration-none.json'));c=d['counters'];print(f\"  {c['requests']} phantom requests -> {c['db_loads']} database queries\")"

log "6/10  What that looks like in the Postgres log"
# Non-fatal on purpose: a missing log excerpt is a worse capture, not a failed
# one. Each statement gets its own psql call -- ALTER SYSTEM cannot run inside
# the single transaction that a multi-statement -c would wrap it in.
sql() { dc exec -T postgres psql -U sysense -d sysense -q -c "$1" >/dev/null 2>&1 || true; }
{
  echo "turning on log_statement=all for a 150-request burst of phantom ids"
  sql "ALTER SYSTEM SET log_statement='all';"
  sql "SELECT pg_reload_conf();"
  sleep 1
  for i in $(seq 1 150); do curl -sS -o /dev/null "localhost:8000/api/users/-$((9000 + i))" || true; done
  sleep 1
  sql "ALTER SYSTEM RESET log_statement;"
  sql "SELECT pg_reload_conf();"

  hits=$(dc logs postgres --tail 2000 2>/dev/null | grep -cF "parameters: \\$1 = '-9" || true)
  echo
  echo "Postgres logged $hits lookups for ids that do not exist. Every one of them"
  echo "is a request the cache was supposed to absorb. An excerpt:"
  echo
  dc logs postgres --tail 2000 2>/dev/null \
    | grep -vE 'pg_advisory_unlock_all|CLOSE ALL|UNLISTEN|RESET ALL|SIGHUP|^\s*$' \
    | grep -E 'execute __asyncpg|SELECT id, name|FROM   users|WHERE  id = |parameters: ' \
    | tail -20
} 2>&1 | tee "$OUT/05-penetration-pg-log.log" || true

log "7/10  PENETRATION defended: null cache, then the Bloom filter"
{
  echo "--- null_cache, a SMALL pool of repeated phantom ids: the easy case"
} | tee "$OUT/06-penetration-defended.log"
defense null_cache 0
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
arm
k6run penetration.js 06a-penetration-null-repeat "MODE=repeat" "POOL=50" "VUS=$PEN_VUS" "DURATION=$PEN_DURATION" "LABEL=null_cache-repeat"
snap penetration-null-repeat

echo "--- null_cache, a DIFFERENT phantom id every time: the real attack" | tee -a "$OUT/06-penetration-defended.log"
defense null_cache 0
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
arm
k6run penetration.js 06b-penetration-null-random "MODE=random" "VUS=$PEN_VUS" "DURATION=$PEN_DURATION" "LABEL=null_cache-random"
snap penetration-null-random

echo "--- bloom filter, the same random attack" | tee -a "$OUT/06-penetration-defended.log"
defense bloom 0
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
arm
k6run penetration.js 06c-penetration-bloom "MODE=random" "VUS=$PEN_VUS" "DURATION=$PEN_DURATION" "LABEL=bloom-random"
snap penetration-bloom

# ── Failure 2: Avalanche ─────────────────────────────────────────────────────

log "8/10  AVALANCHE: $KEYS keys, one TTL, one expiry instant"
defense none 0
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
post localhost:8000/api/cache/warm \
  -d "{\"count\":$KEYS,\"ttl\":$WARM_TTL,\"jitter\":0}" | tee "$OUT/07a-warm-fixed.json"; echo
api "localhost:8000/api/cache/ttls" > "$OUT/07a-ttls-fixed.json"
arm
cpu_start avalanche-fixed
k6run avalanche.js 07a-avalanche-fixed "KEYS=$KEYS" "RPS=$AV_RPS" "DURATION=$AV_DURATION" "LABEL=avalanche-fixed-ttl"
cpu_stop
snap avalanche-fixed
py "import json;d=json.load(open('$OUT/s-avalanche-fixed.json'));print(f\"  peak {d['peak_db_loads_per_s']} database queries/sec\")"

log "9/10  AVALANCHE with TTL jitter: the same traffic, the same keys"
defense none "$JITTER"
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
post localhost:8000/api/cache/warm \
  -d "{\"count\":$KEYS,\"ttl\":$WARM_TTL,\"jitter\":$JITTER}" | tee "$OUT/07b-warm-jitter.json"; echo
api "localhost:8000/api/cache/ttls" > "$OUT/07b-ttls-jitter.json"
arm
cpu_start avalanche-jitter
k6run avalanche.js 07b-avalanche-jitter "KEYS=$KEYS" "RPS=$AV_RPS" "DURATION=$AV_DURATION" "LABEL=avalanche-ttl-jitter"
cpu_stop
snap avalanche-jitter
py "import json;d=json.load(open('$OUT/s-avalanche-jitter.json'));print(f\"  peak {d['peak_db_loads_per_s']} database queries/sec\")"

# ── Failure 3: Breakdown -- named, demoed, and left broken ───────────────────

log "10/10  BREAKDOWN: one hot key, ${HOT_RPS} req/sec, and it expires at t+${HOT_EXPIRE_AT}s"
defense none 0
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
curl -fsS -o /dev/null localhost:8000/api/users/42     # warm the one key
arm
cpu_start breakdown
( sleep "$HOT_EXPIRE_AT"; post localhost:8000/api/cache/expire -d '{"uid":42}' >/dev/null ) &
EXPIRER=$!
k6run breakdown.js 08-breakdown "HOT=42" "RPS=$HOT_RPS" "DURATION=$HOT_DURATION" \
  "MAX_VUS=4000" "LABEL=breakdown-hot-key"
wait "$EXPIRER" 2>/dev/null || true
cpu_stop
snap breakdown
py "import json;d=json.load(open('$OUT/m-breakdown.json'));print(f\"  one key expired -> {d['max_concurrent_db_loads']} identical queries in flight at once\")"

log "Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
