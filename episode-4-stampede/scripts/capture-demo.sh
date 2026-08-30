#!/usr/bin/env bash
#
# Runs the Episode 4 demo end to end and records what actually happened.
#
# One hot key, two hundred readers, and the instant it expires. First with
# nothing coordinating them, then with a lock, then with the lock released
# unsafely, then with the Lua release, then with XFetch -- which arranges for
# the instant never to arrive.
#
#   ./scripts/capture-demo.sh
#
# Writes:  capture/*.log, capture/*.json  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
mkdir -p "$OUT"

# ── The dials. Defaults are what the episode was captured with. ──────────────
HOT=${HOT:-42}                  # the hot key: one viral profile
VUS=${VUS:-200}                 # readers released at the same instant
KEYS=${KEYS:-5000}              # keys the ceiling measurement spreads over
LOCK_TTL_MS=${LOCK_TTL_MS:-5000}   # comfortably longer than a rebuild
SHORT_LOCK_MS=${SHORT_LOCK_MS:-10} # deliberately shorter than a rebuild
SUS_RPS=${SUS_RPS:-2000}        # sustained traffic on the one hot key
SUS_DURATION=${SUS_DURATION:-60s}
SUS_TTL=${SUS_TTL:-5}           # so the key expires ~12 times during that run
REL_DURATION=${REL_DURATION:-20s}
REL_TTL=${REL_TTL:-3}
BETA=${BETA:-1.0}               # XFetch eagerness; 1.0 is the paper's default

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

# stampede <none|lock|xfetch> [lock_ttl_ms] [lua|unsafe] [cache_ttl_s]
stampede() {
  post localhost:8000/api/stampede \
    -d "{\"stampede\":\"$1\",\"lock_ttl_ms\":${2:-$LOCK_TTL_MS},\"lock_release\":\"${3:-lua}\",\"beta\":$BETA,\"cache_ttl_seconds\":${4:-300}}" \
    | tr -d '\r'; echo
}

# Postgres CPU, sampled while an attack runs. Backgrounded; stopped with cpu_stop.
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

# One stampede: warm the key, arm the counters, release VUS readers at the
# instant k6's setup() expires it.
burst() {                     # burst <name> <phase-label>
  local name=$1 label=$2
  # Let the previous phase drain. One captured run had a rebuild take ten times
  # its usual 25 ms because the burst began while the phase before it was still
  # finishing, and a single outlier is the whole measurement when the run is one
  # query long.
  sleep 5
  curl -fsS -o /dev/null "localhost:8000/api/users/$HOT"      # warm the one key
  arm
  cpu_start "$label"
  k6run stampede.js "$name" "HOT=$HOT" "VUS=$VUS" "LABEL=$label"
  cpu_stop
  snap "$label"
}

# The same event with log_statement=all, so the claim is Postgres' own log and
# not the application's word for it. Kept separate from the measured run: 200
# logged statements are a real cost and would show up in the latencies.
pg_log_burst() {              # pg_log_burst <out-name> <phase> <heading>
  local name=$1 phase=$2 heading=$3
  sql() { dc exec -T postgres psql -U sysense -d sysense -q -c "$1" >/dev/null 2>&1 || true; }
  lines() { dc logs postgres 2>/dev/null | wc -l | tr -d ' '; }
  {
    echo "== $heading"
    curl -fsS -o /dev/null "localhost:8000/api/users/$HOT"   # warm, before logging
    sql "ALTER SYSTEM SET log_statement='all';"
    sql "SELECT pg_reload_conf();"
    sleep 1

    # Everything the burst added to the log, and nothing else. Sliced by line
    # count rather than by timestamp: the container's clock is UTC and this
    # machine's is not, and a log line missed by an hour is a silent zero.
    local before after added
    arm                       # so the app's own count covers this burst alone
    before=$(lines)
    dc run --rm -e "BASE=http://app:8000" -e "HOT=$HOT" -e "VUS=$VUS" \
      -e "LABEL=$name" load-test run /scripts/stampede.js >/dev/null 2>&1 || true
    sleep 1
    after=$(lines)
    added=$((after - before))
    # Sliced first, and only then anything else: `snap` reads pg_stat_statements,
    # which log_statement=all logs too, and those lines would push the window
    # past the single execution the locked run exists to show.
    dc logs postgres --tail "$added" 2>/dev/null > "$OUT/$name-raw.log"
    sql "ALTER SYSTEM RESET log_statement;"
    sql "SELECT pg_reload_conf();"
    snap "$phase"

    local aggregates statements
    aggregates=$(grep -c 'WITH spend AS' "$OUT/$name-raw.log" || true)
    statements=$(grep -cF "parameters: \$1 = '$HOT'" "$OUT/$name-raw.log" || true)
    echo
    echo "$VUS readers, one key, one expiry. Postgres logged $added lines."
    echo "$aggregates of them are executions of the profile aggregate, and"
    echo "$statements are statements carrying \$1 = '$HOT'."
    echo
    grep -vE 'pg_advisory_unlock_all|CLOSE ALL|UNLISTEN|RESET ALL|SIGHUP' "$OUT/$name-raw.log" \
      | grep -E 'execute __asyncpg|WITH spend AS|parameters: ' | head -10
    echo "PG_LOG_COUNT {\"run\":\"$name\",\"log_lines\":$added,\"aggregates\":$aggregates,\"statements_for_hot_key\":$statements}"
  } 2>&1 | tee "$OUT/$name-pg-log.log" || true
  grep '^PG_LOG_COUNT ' "$OUT/$name-pg-log.log" | sed 's/^PG_LOG_COUNT //' \
    > "$OUT/$name-pg-log.json" || true
}

# ═══════════════════════════════════════════════════════════════════════════

log "1/12  Tearing down any previous run"
dc down -v --remove-orphans >/dev/null 2>&1 || true

log "2/12  Starting the stack -- STAMPEDE_DEFENSE=none, which is the bug"
STAMPEDE_DEFENSE=none LOCK_TTL_MS=$LOCK_TTL_MS dc up --build -d 2>&1 | tee "$OUT/01-compose-up.log"
wait_healthy
api localhost:8000/health > "$OUT/health.json"
py "import json;d=json.load(open('$OUT/health.json'));print(f\"  stampede={d['stampede']} lock_ttl={d['lock_ttl_ms']}ms release={d['lock_release']} beta={d['xfetch_beta']}\")"

log "3/12  Episodes 1 to 3 still work  (the callback, re-measured here)"
{
  for i in $(seq 1 20); do
    curl -fsS -D - -o /dev/null "localhost:8000/api/users/$HOT" \
      | awk -v i="$i" '
          tolower($1) ~ /^x-cache:/      { c=$2 }
          tolower($1) ~ /^x-elapsed-ms:/ { e=$2 }
          END { printf "req %-3d  %-5s %8.2f ms\n", i, c, e }' | tr -d '\r'
  done
  curl -fsS -D - -o /dev/null "localhost:8000/api/users/$HOT/uncached" \
    | awk 'tolower($1) ~ /^x-elapsed-ms:/ { printf "uncached      %8.2f ms\n", $2 }' | tr -d '\r'
} 2>&1 | tee "$OUT/02-baseline-reads.log"

log "4/12  What Postgres can take, and what Redis can  (the two ceilings)"
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
arm
k6run ceiling.js 03-db-ceiling "KEYS=$KEYS"
snap ceiling
py "import json;d=json.load(open('$OUT/03-db-ceiling.json'));print(f\"  Postgres serves {d['rps']} profile aggregates/sec, median {d['latency_ms']['med']} ms\")"

post localhost:8000/api/cache/warm -d "{\"count\":$KEYS,\"ttl\":300,\"jitter\":0}" >/dev/null
arm
k6run ceiling.js 03b-cache-ceiling "MODE=cache" "KEYS=$KEYS" "RPS=2500" "DURATION=10s"
py "import json;d=json.load(open('$OUT/03b-cache-ceiling.json'));print(f\"  Redis serves {d['rps']} req/sec at {d['latency_ms']['med']} ms median\")"

# ── The stampede itself ──────────────────────────────────────────────────────

log "5/12  STAMPEDE, undefended: $VUS readers, one key, the instant it expires"
stampede none "$LOCK_TTL_MS" lua 300
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
burst 04-stampede-none stampede-none
py "import json;m=json.load(open('$OUT/m-stampede-none.json'));c=m['counters'];print(f\"  {c['requests']} readers -> {c['db_loads']} database queries, {m['max_concurrent_db_loads']} in flight at once\")"

log "6/12  The same event in the Postgres log"
pg_log_burst 05-pg-log-none pg-none "undefended: every reader ran the aggregate"

log "7/12  STAMPEDE, with the lock: identical run, one line of Redis different"
stampede lock "$LOCK_TTL_MS" lua 300
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
burst 06-stampede-lock stampede-lock
py "import json;m=json.load(open('$OUT/m-stampede-lock.json'));c=m['counters'];print(f\"  {c['requests']} readers -> {c['db_loads']} database query, {c['wait_hits']} waited and read the cache\")"

log "8/12  And in the Postgres log"
pg_log_burst 07-pg-log-lock pg-lock "with the lock: one execution, for two hundred readers"

# ── The bug most tutorials skip ──────────────────────────────────────────────
#
# A lock whose TTL is shorter than the rebuild expires while its holder is
# still working. Sustained traffic keeps arriving, so the next reader to miss
# takes the free lock -- and then the first holder finishes and deletes it.

log "9/12  UNSAFE RELEASE: lock TTL ${SHORT_LOCK_MS} ms, shorter than the rebuild"
stampede lock "$SHORT_LOCK_MS" unsafe "$REL_TTL"
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
curl -fsS -o /dev/null "localhost:8000/api/users/$HOT"
arm
k6run sustained.js 08-release-unsafe "HOT=$HOT" "RPS=$SUS_RPS" "DURATION=$REL_DURATION" "LABEL=release-unsafe"
snap release-unsafe
py "import json;m=json.load(open('$OUT/m-release-unsafe.json'));c=m['counters'];print(f\"  {c['release_wrongful']} locks deleted that belonged to somebody else\")"

log "10/12  SAFE RELEASE: the same short TTL, compare-and-delete in Lua"
stampede lock "$SHORT_LOCK_MS" lua "$REL_TTL"
post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
curl -fsS -o /dev/null "localhost:8000/api/users/$HOT"
arm
k6run sustained.js 09-release-lua "HOT=$HOT" "RPS=$SUS_RPS" "DURATION=$REL_DURATION" "LABEL=release-lua"
snap release-lua
py "import json;m=json.load(open('$OUT/m-release-lua.json'));c=m['counters'];print(f\"  {c['release_wrongful']} wrongful deletes, {c['release_refused']} releases refused\")"

# ── Sustained traffic: the same key expiring over and over ───────────────────

log "11/12  SUSTAINED: $SUS_RPS req/sec for $SUS_DURATION, TTL ${SUS_TTL}s, three ways"
for mode in none lock xfetch; do
  echo "--- $mode"
  stampede "$mode" "$LOCK_TTL_MS" lua "$SUS_TTL"
  post localhost:8000/api/cache/expire -d '{"all":true}' >/dev/null
  curl -fsS -o /dev/null "localhost:8000/api/users/$HOT"   # warm, and write XFetch's metadata
  arm
  cpu_start "sustained-$mode"
  k6run sustained.js "10-sustained-$mode" "HOT=$HOT" "RPS=$SUS_RPS" \
    "DURATION=$SUS_DURATION" "LABEL=sustained-$mode"
  cpu_stop
  snap "sustained-$mode"
  py "import json;m=json.load(open('$OUT/m-sustained-$mode.json'));c=m['counters'];print(f\"  {c['db_loads']} rebuilds, {c['early_refreshes']} of them early, {m['max_waiting_on_lock']} readers waiting at the peak\")"
done

log "12/12  Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
