#!/usr/bin/env bash
#
# Runs the Episode 2 demo end to end and records what actually happened.
#
# Everything the episode claims on screen comes out of this script. If a number
# changes when you run it on your machine, the number was real.
#
#   ./scripts/capture-demo.sh
#
# Writes:  capture/*.log, capture/race-*.json  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
N=${N:-20}          # race iterations per policy
B=${B:-20}          # baseline read samples
STALL=${STALL:-400} # how far writer A is delayed between its two writes
LAG=${LAG:-150}     # how long after writer A that writer B starts
mkdir -p "$OUT"

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
dc()  { docker compose "$@"; }

wait_healthy() {
  printf 'waiting for app '
  for _ in $(seq 1 90); do
    if curl -fsS localhost:8000/health >/dev/null 2>&1; then echo ' ready'; return 0; fi
    printf '.'; sleep 1
  done
  echo ' TIMED OUT'; dc logs app | tail -40; return 1
}

policy() {   # restart the app under a different write policy
  WRITE_POLICY="$1" WRITE_BEHIND_FLUSH_MS="${2:-1000}" dc up -d --no-deps app >/dev/null 2>&1
  wait_healthy
  curl -fsS localhost:8000/health | tr -d '\r'; echo
}

reads() {    # B reads of the cached endpoint, one line each
  local file=$1
  : > "$file"
  for i in $(seq 1 "$B"); do
    curl -fsS -D - -o /dev/null "localhost:8000/api/users/42" \
      | awk -v i="$i" '
          tolower($1) ~ /^x-cache:/      { c=$2 }
          tolower($1) ~ /^x-elapsed-ms:/ { e=$2 }
          END { printf "req %-3d  %-5s %8.2f ms\n", i, c, e }
        ' | tr -d '\r' | tee -a "$file"
  done
}

log "1/9  Tearing down any previous run"
dc down -v --remove-orphans >/dev/null 2>&1 || true

log "2/9  Starting the stack -- cache ON, WRITE_POLICY=update"
WRITE_POLICY=update dc up --build -d 2>&1 | tee "$OUT/01-compose-up.log"
wait_healthy

log "3/9  Episode 1's read path, still working  (${B}x cached, then one uncached)"
{
  reads "$OUT/tmp-reads.log"
  curl -fsS -D - -o /dev/null "localhost:8000/api/users/42/uncached" \
    | awk 'tolower($1) ~ /^x-elapsed-ms:/ { printf "uncached      %8.2f ms\n", $2 }' | tr -d '\r'
} 2>&1 | tee "$OUT/02-baseline-reads.log"
rm -f "$OUT/tmp-reads.log"

log "4/9  The nightmare: WRITE_POLICY=update, ${N} concurrent-write races"
python3 scripts/race.py --iterations "$N" --stall "$STALL" --lag "$LAG" \
  --json-out "$OUT/race-update.json" 2>&1 | tee "$OUT/03-race-update.log"

log "5/9  How long does a stale key stay wrong?  (nothing repairs it)"
{
  echo "the cache and the database have just disagreed. watching them not fix it:"
  prev=0
  for t in 0 5 15 30; do
    if [ "$t" -gt "$prev" ]; then sleep $((t - prev)); fi
    prev=$t
    curl -fsS localhost:8000/api/users/42/truth \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"  t+{$t:>2}s  db={d['db_city']:<9} cache={str(d['cache_city']):<9} agree={d['agree']!s:<5} ttl={d['ttl']}s\")"
  done
  echo
  echo "nothing above is a bug report, a retry or an alarm. it is just wrong."
} 2>&1 | tee "$OUT/04-stale-window.log"

log "6/9  The fix: WRITE_POLICY=delete, the same ${N} races"
policy delete | tee "$OUT/tmp-policy.log"
python3 scripts/race.py --iterations "$N" --stall "$STALL" --lag "$LAG" \
  --json-out "$OUT/race-delete.json" 2>&1 | tee "$OUT/05-race-delete.log"

log "7/9  WRITE_POLICY=write_through, the same ${N} races"
policy write_through >/dev/null
python3 scripts/race.py --iterations "$N" --stall "$STALL" --lag "$LAG" \
  --json-out "$OUT/race-write-through.json" 2>&1 | tee "$OUT/06-race-write-through.log"

log "8/9  WRITE_POLICY=write_behind, the same ${N} races, then a lost write"
policy write_behind 1000 >/dev/null
python3 scripts/race.py --iterations "$N" --stall "$STALL" --lag "$LAG" --flush-wait 1.5 \
  --json-out "$OUT/race-write-behind.json" 2>&1 | tee "$OUT/07-race-write-behind.log"

{
  echo "write-behind, flush interval 10s: what happens if the process dies first"
  policy write_behind 10000 >/dev/null
  curl -fsS -X POST localhost:8000/api/users/42/reset \
       -H 'content-type: application/json' -d '{"city":"Lisbon"}' >/dev/null
  curl -fsS localhost:8000/api/users/42 >/dev/null
  curl -fsS -X PUT localhost:8000/api/users/42 \
       -H 'content-type: application/json' -d '{"city":"Berlin"}' >/dev/null
  echo "  wrote city=Berlin. queued, not yet flushed:"
  curl -fsS localhost:8000/api/users/42/truth \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"    db={d['db_city']:<9} cache={d['cache_city']:<9} queue_depth={d['queue_depth']}\")"
  echo "  killing the app before the flusher runs"
  docker compose kill app >/dev/null 2>&1
  policy write_behind 10000 >/dev/null
  echo "  after restart:"
  curl -fsS localhost:8000/api/users/42/truth \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"    db={d['db_city']:<9} cache={d['cache_city']:<9} agree={d['agree']}\")"
  echo "  the write is gone from Postgres. the cache is the only place it ever existed."
} 2>&1 | tee "$OUT/08-write-behind-loss.log"

log "9/9  What correctness costs: the read straight after a write"
{
  for p in delete write_through; do
    policy "$p" >/dev/null
    echo "policy=$p"
    for i in 1 2 3 4 5; do
      curl -fsS -X POST localhost:8000/api/users/42/reset \
           -H 'content-type: application/json' -d '{"city":"Lisbon"}' >/dev/null
      curl -fsS localhost:8000/api/users/42 >/dev/null            # warm
      curl -fsS -X PUT localhost:8000/api/users/42 \
           -H 'content-type: application/json' -d '{"city":"Berlin"}' >/dev/null
      curl -fsS -D - -o /dev/null localhost:8000/api/users/42 \
        | awk -v p="$p" '
            tolower($1) ~ /^x-cache:/      { c=$2 }
            tolower($1) ~ /^x-elapsed-ms:/ { e=$2 }
            END { printf "  first read after write   %-5s %8.2f ms\n", c, e }
          ' | tr -d '\r'
      curl -fsS -D - -o /dev/null localhost:8000/api/users/42 \
        | awk '
            tolower($1) ~ /^x-cache:/      { c=$2 }
            tolower($1) ~ /^x-elapsed-ms:/ { e=$2 }
            END { printf "  second read              %-5s %8.2f ms\n", c, e }
          ' | tr -d '\r'
    done
  done
} 2>&1 | tee "$OUT/09-cost-of-correctness.log"
rm -f "$OUT/tmp-policy.log"

log "Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
