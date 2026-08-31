# Episode 4 — The Boss Fight

**▶ Watch the episode:** <https://youtu.be/zlscItyxQdw>

Cache Stampede, a distributed lock, the release bug most tutorials skip, and the
arithmetic that stops the stampede from having a moment to happen in.

Episode 1 made reads fast. Episode 2 made writes correct. Episode 3 attacked the
result with traffic and fixed two of the three failures it found. It named the
third — **Cache Breakdown**, one hot key expiring under concurrent load — and
deliberately left it broken. This is that fix, on the same app.

Everything below was measured by `./scripts/capture-demo.sh` on the machine that
recorded the episode. Re-run it and you will get your own numbers.

```bash
docker compose up --build
```

Then open <http://localhost:8080> and press the buttons.

---

## The event

One key. `user:42`, a viral profile. Two hundred readers already reading it, and
then it expires underneath them.

Nothing in Cache-Aside says *"somebody else is already fetching this"*, so every
reader that misses runs the same expensive aggregate:

```
200 readers, one key, one expiry

  undefended    129 database queries      127 in flight at once
                                          all 20 pool connections held
                slowest reader 400.18 ms

  with a lock     1 database query          1 in flight
                199 readers waited for it and read what it wrote
                slowest reader  57.03 ms
```

**129 queries became 1. The slowest reader got 7× faster.** Same traffic, same
key, same instant — one line of Redis in between.

Postgres agrees, in its own log. The same burst again with `log_statement=all`:

| | log lines | executions of the aggregate |
| --- | ---: | ---: |
| undefended | 1,800 | **50** |
| with the lock | 36 | **1** |

(The logged runs are separate from the measured ones — logging every statement
is a real cost and would show up in the latencies above. In the logged run the
app counted 50 rebuilds and Postgres logged 50 executions: the two agree
exactly, which is the point of doing it twice.)

## What this app can take

Every claim here is relative to a measured ceiling, so none of them are
adjectives:

| | |
| --- | --- |
| Cache hit | **0.26 ms** |
| Uncached profile read | **26.66 ms** |
| Postgres, saturated | **261.3 profile aggregates/sec** |
| The app, serving from Redis | **2,399.9 req/sec**, nothing dropped |

---

## The lock, in three lines

```python
token = uuid4().hex
if await redis.set(f"lock:{key}", token, nx=True, px=LOCK_TTL_MS):
    ...                     # you are the one who rebuilds
```

`NX` means *only if it does not exist*: exactly one of two hundred concurrent
callers gets `True`. `PX` is not a detail — it is what stops a worker that dies
mid-rebuild from locking the key forever.

The other 199 do not queue at the database. They sleep for a few milliseconds
and read what the winner wrote:

```
leader   36.65 ms   ran the query
waiters  44.18 ms   median, 57.03 ms at the worst
```

### The release, which is where it goes wrong

Giving the lock back looks like the easy half. It is not:

1. Worker **A** takes the lock with a 10 ms TTL.
2. A's rebuild takes 25 ms, so the lock expires while A is still working, and
   Redis hands it to worker **B**.
3. A finishes and deletes "the lock". **It deletes B's.**

Now B is rebuilding with no lock, C acquires immediately, and the stampede is
back with an extra step. Run it both ways at a 10 ms lock TTL — 40,000 requests
each, everything else identical:

| Release | Rebuilds | Locks deleted that were not theirs | Releases refused |
| --- | ---: | ---: | ---: |
| `DEL` | 17 | **5** | 0 |
| Lua compare-and-delete | 17 | **0** | 17 |

```lua
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
```

Redis runs a script atomically, so the compare and the delete cannot be
separated — not by another client, and not by your own lock expiring between the
two.

**Lua does not make a too-short TTL correct.** The lock still expires early
either way; that is why the refused count is 17 rather than 0. What it stops is
that expiry turning into one worker deleting another's lock. The TTL is the
thing to fix — see the experiment below.

---

## XFetch: never have the moment at all

A lock *survives* the expiry. Probabilistic early refresh arranges for there not
to be one. On the way out of a cache hit, every reader rolls:

```
β · δ · −ln(rand())  ≥  TTL remaining      →  rebuild it now
```

`δ` is what the last rebuild actually cost, `β` tunes eagerness. `−ln(rand())`
is an exponential draw, so a reader is more likely to volunteer the closer the
key is to expiry and the more expensive it is to rebuild. Expensive keys get
refreshed earlier than cheap ones without anyone configuring that.

Same key, 2,000 req/sec for 60 seconds, TTL 5 s — so it expires twelve times:

| | none | lock | xfetch |
| --- | ---: | ---: | ---: |
| Requests | 118,458 | 119,618 | 119,298 |
| Database queries | **891** | **12** | **12** |
| Concurrent queries, peak | **150** | 1 | 1 |
| Postgres connections held, peak | **20 of 20** | 1 | 1 |
| Readers who had to wait | 0 | 571 | **0** |
| Rebuilds that happened *before* expiry | 0 | 0 | **12** |
| p99 latency | 129.8 ms | 2.01 ms | **2.4 ms** |
| Postgres CPU, peak | **376.9%** | 4.6% | 4.8% |

Read the "readers who had to wait" row. The lock is a good answer: twelve
rebuilds instead of 891, and Postgres never notices. But 571 readers still sat
waiting for one of them. Under XFetch nobody waited at all, because the key was
rebuilt in the background twelve times while the old value was still being
served — **it never once expired under load**.

That is the difference between surviving the stampede and not having one.

---

## The knobs

```bash
# The bug (the default), the lock, or the dice
STAMPEDE_DEFENSE=lock docker compose up --build
STAMPEDE_DEFENSE=xfetch docker compose up --build

# Episode 3's two are still here and still work
PENETRATION_DEFENSE=bloom TTL_JITTER_SECONDS=300 docker compose up --build
```

| Variable | Default | |
| --- | --- | --- |
| `STAMPEDE_DEFENSE` | `none` | `none` \| `lock` \| `xfetch` |
| `LOCK_TTL_MS` | `5000` | how long one rebuild may hold the key |
| `LOCK_WAIT_MS` | `5000` | how long a loser waits before giving up and going to Postgres itself |
| `LOCK_RELEASE` | `lua` | `lua` \| `unsafe` |
| `XFETCH_BETA` | `1.0` | higher refreshes earlier |

### Try this — the lock timeout

**Set `LOCK_TTL_MS` below the time a rebuild takes**, which on this machine is
about 25 ms:

```bash
STAMPEDE_DEFENSE=lock LOCK_TTL_MS=10 LOCK_RELEASE=unsafe docker compose up --build
docker compose run --rm -e RPS=2000 -e DURATION=20s load-test run /scripts/sustained.js
curl -s localhost:8000/metrics | grep -A2 release_wrongful
```

Watch `release_wrongful` climb. Then run it again with `LOCK_RELEASE=lua` and
watch the same situation become `release_refused` instead — nobody's lock gets
deleted by somebody else. Then put `LOCK_TTL_MS` back above the rebuild time and
watch both counters go to zero, which is the actual fix.

## The full-sized attacks

The buttons on the page fire a few hundred requests so they finish while you
watch. These are the real ones:

```bash
docker compose run --rm load-test run /scripts/stampede.js    # 200 readers, one expiry
docker compose run --rm load-test run /scripts/sustained.js   # the same key, left running
docker compose run --rm load-test run /scripts/ceiling.js     # not an attack, a measurement

# Episode 3's, still here and still working
docker compose run --rm load-test run /scripts/penetration.js
docker compose run --rm load-test run /scripts/avalanche.js
docker compose run --rm load-test run /scripts/breakdown.js
```

Or run the whole capture, exactly as the episode did:

```bash
./scripts/capture-demo.sh        # → capture/metrics.json
```

## What is where

```
app/stampede.py    The lock, the Lua release, and the XFetch roll. Start here.
app/cache.py       Cache-Aside, with every episode's fix folded into it.
app/metrics.py     The counters. db_loads is incremented in exactly one place.
app/main.py        The API and the evidence endpoints.
load-test/         The k6 attacks.
scripts/           capture-demo.sh, and the summariser that writes metrics.json.
capture/           What the last run measured. metrics.json is the source of truth.
```

Nothing here is estimated. The app counts its own database calls, Postgres counts
them independently with `pg_stat_statements`, and for the headline event Postgres
logs every statement it ran — three accounts of the same number.

## One honest caveat

This stack runs **a single application process**, because that is the stack
Episode 1 settled on and every episode since has extended in place. With one
process you could coordinate those 200 readers with an in-process
`asyncio.Lock` and never involve Redis at all.

The moment there are two processes, you cannot. Nothing in worker 1 knows what
worker 2 is doing, and the two of them will happily run the same rebuild at the
same time. That is why the lock lives in Redis: the code above is unchanged
whether you run one process or forty, and it is the only version of it that is
still correct at forty.

---

**◀ Previous:** [Episode 3 — The 3 Classic Failures](../episode-3-failures/) ·
**The series:** [start at Episode 1](../episode-1-basics/)
