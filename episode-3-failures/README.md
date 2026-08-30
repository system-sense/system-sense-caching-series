# Episode 3 — The 3 Classic Failures

Penetration, Avalanche and Breakdown are **three different bugs with three
different fixes**. The most common mistake is collapsing them into one vague
worry about "cache problems", and then reaching for the wrong tool.

Episode 1 made reads fast. Episode 2 made writes correct. This one keeps that
exact app — invalidation and all — and attacks it with *traffic*.

Everything below was measured by `./scripts/capture-demo.sh` on the machine that
recorded the episode. Re-run it and you will get your own numbers.

```bash
docker compose up --build
```

Then open <http://localhost:8080> and press the attack buttons.

---

## The one table to remember

| Failure | Which keys? | Root cause | Fix |
| --- | --- | --- | --- |
| **Penetration** | Keys that **never** exist | No negative caching | Null caching / Bloom filter |
| **Avalanche** | **Many** keys at once | Correlated TTLs | TTL jitter |
| **Breakdown** | **One** hot key | Expiry under concurrency | → Episode 4 |

## What this app can take

Every claim below is relative to one measured ceiling, so none of them are
adjectives:

| | |
| --- | --- |
| Cache hit | **0.15 ms** |
| Uncached profile read | **21.24 ms** |
| Postgres, saturated | **316.5 profile aggregates/sec** |
| The app, serving from Redis | **2,499.9 req/sec at 0.42 ms**, nothing dropped |

That last row is an offered rate that was comfortably met, not a limit anyone
found — Redis is bored at **eight times** the rate Postgres cannot survive. That
gap is the whole episode.

---

## Failure 1 — Penetration

Requests for ids that were never issued. Redis has never heard of them, Postgres
has nothing to return, and there is **nothing to cache afterwards** — so every
request goes all the way to disk. The cache is not failing. It is being walked
past.

```
119,509 phantom requests  →  119,509 database queries      100.0%
```

Postgres' own `pg_stat_statements` agrees to the query: 119,509 executions of
the user lookup. In its log, that is the same four lines over and over:

```
LOG:  execute __asyncpg_stmt_21__:
        SELECT id, name, email, city, joined_at
        FROM   users
        WHERE  id = $1
DETAIL:  parameters: $1 = '-9148'
```

### Two defenses, and why you need the second one

**Null caching** stores a tombstone for an id that turned out not to exist, with
a short TTL (30 s here — a tombstone is a guess about a row that might be
created tomorrow).

**A Bloom filter** answers "definitely not here" from local memory, before Redis
and before Postgres. All 100,000 ids fit in **117 KiB** with 7 hashes, built in
161 ms at startup.

Same attack, three configurations:

| Defense | Attack | Requests | Reached Postgres | |
| --- | --- | ---: | ---: | ---: |
| none | random ids | 119,509 | 119,509 | **100%** |
| null_cache | 50 repeated ids | 208,138 | 82 | **0.04%** |
| null_cache | random ids | 104,747 | 99,593 | **95.08%** |
| bloom | random ids | 299,468 | 2,611 | **0.87%** |

Read the third row twice. **Null caching barely helps against the real attack**,
because a new phantom id has no tombstone yet — it costs its own query first.
It only looks like a fix when the attacker is lazy enough to reuse ids.

The Bloom filter's 0.87% is not leakage, it is the price on the ticket: those
2,611 requests are exactly its false positives, against a 1% design target and a
1% predicted rate. The maths held.

---

## Failure 2 — Avalanche

A batch job warms 5,000 keys in one pass — one aggregate query, 5,000 Redis
writes, **106 ms** for the lot. Every key gets the same TTL, so every key reaches
the end of it in the same instant.

Then ordinary traffic, 600 req/sec, nothing hostile, across that instant:

```
t=0-24s   600 req/s     all hits      0.69 ms      0 database queries
t=25s     ─────────── every key expires at once ───────────
t=25s+    600 req/s     all misses    2,204 ms     588 queries/sec
```

The offered rate never changed. Only where it landed did.

| | Fixed TTL | TTL + rand(0,30) |
| --- | ---: | ---: |
| TTL spread | 0 s | 30 s |
| Peak database queries/sec | **588** | **196** |
| Rebuild latency, median | **2,204.86 ms** | **22.96 ms** |
| p95, all requests | **3,453.93 ms** | **23.33 ms** |
| Slowest request | **10,727.51 ms** | **57.37 ms** |
| Postgres CPU, peak | **1207.2%** | **389.9%** |

**p95 improved 148×.** Same keys, same traffic, same wait. The difference is one
line of arithmetic:

```python
TTL = base_ttl + random.randint(0, jitter)
```

(Median CPU is not comparable between the two runs: the fixed-TTL run is idle
for its first 25 seconds and then saturated, so its median describes neither
half. The peak is the honest comparison — 12 cores are available, so 1207% is
Postgres using essentially all of them.)

---

## Failure 3 — Breakdown, and this episode does not fix it

One key. A viral profile, read by everybody at once. While it is cached this is
the cheapest traffic in the world:

```
2,000 req/sec   49,923 hits   0.40 ms median
```

Then that single key expires. Not many keys — **one**:

```
79 database queries for one key, where 1 would have done
78 of them in flight simultaneously
each rebuild 185.44 ms, against the ~21 ms that query costs when run alone
```

Every reader in flight missed, and every one of them ran the same expensive
aggregate, because **nothing in Cache-Aside says "somebody else is already
fetching this"**. Worse, it feeds itself: 78 copies of one query make each copy
slower, which widens the window, which admits more copies.

TTL jitter cannot fix this — there is only one key, and jitter spreads *many*.
Null caching cannot fix it — the row exists. The fix is a lock that lets one
reader through and makes the rest wait, and that is
**[Episode 4](../episode-4-stampede/)**.

---

## The knobs

Both fixes are environment variables. Change one and re-run the same attack:

```bash
# Penetration: none (the bug) | null_cache | bloom
PENETRATION_DEFENSE=bloom docker compose up --build

# Avalanche: 0 (the bug) | any number of seconds
TTL_JITTER_SECONDS=300 docker compose up --build

# Both at once
PENETRATION_DEFENSE=bloom TTL_JITTER_SECONDS=300 docker compose up --build
```

**Try this:** set `PENETRATION_DEFENSE=null_cache` and run the penetration attack
twice — once with *50 repeated ids*, once with *random ids*. The same defense
stops 99.96% of one and 5% of the other. That difference is the entire argument
for the Bloom filter, and it takes about twenty seconds to see for yourself.

## The full-sized attacks

The buttons on the page fire a few thousand requests so they finish while you
watch. These are the real ones:

```bash
docker compose run --rm load-test run /scripts/penetration.js
docker compose run --rm load-test run /scripts/avalanche.js
docker compose run --rm load-test run /scripts/breakdown.js
docker compose run --rm load-test run /scripts/ceiling.js     # not an attack, a measurement
```

Or run the whole capture, exactly as the episode did:

```bash
./scripts/capture-demo.sh        # → capture/metrics.json
```

## What is where

```
app/cache.py       Cache-Aside, with both defenses folded in. Start here.
app/bloom.py       A Bloom filter in forty lines, no dependencies.
app/metrics.py     The counters. db_loads is incremented in exactly one place.
app/main.py        The API, the batch warmer, and the evidence endpoints.
load-test/         The k6 attacks.
scripts/           capture-demo.sh, and the summariser that writes metrics.json.
capture/           What the last run measured. metrics.json is the source of truth.
```

Nothing here is estimated. The app counts its own database calls, Postgres counts
them independently with `pg_stat_statements`, and the two agree — which is why
both are in `capture/`.

---

**◀ Previous:** [Episode 2 — The Invalidation Nightmare](../episode-2-invalidation/) ·
**Next ▶:** [Episode 4 — The Boss Fight](../episode-4-stampede/)
