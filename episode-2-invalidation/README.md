# Episode 2 — The Invalidation Nightmare

> *"There are only two hard things in Computer Science: cache invalidation and
> naming things."* — Phil Karlton

Episode 1 made reads fast. This one adds writes, and watches the cache start
lying.

Everything below was measured by `./scripts/capture-demo.sh` on the machine that
recorded the episode. Re-run it and you will get your own numbers.

```bash
docker compose up --build
```

Then open <http://localhost:8080> and press **Run the race**.

---

## The bug, in four lines

Two requests change the same user at the same time. Both write Postgres, both
write Redis, and they do not finish in the same order:

```
T1  UPDATE db = Toronto
T2  UPDATE db = Berlin
T2  SET cache = Berlin
T1  SET cache = Toronto     ◀── the loser writes last
```

Postgres now says **Berlin**. Redis says **Toronto**. Every reader gets Toronto,
in a fraction of a millisecond, with a `200 OK`. There is no error, no retry and
no alarm — the fastest wrong answer your system will ever serve.

Run it 20 times:

```
20/20 runs left the cache disagreeing with the database  (100.0%)
```

## Why it is reproducible here and rare in production

`stall_ms` widens the window between a writer's two writes. In production that
window is scheduler jitter, a GC pause, a retried packet — microseconds to tens
of milliseconds, hit by two concurrent writers rarely enough to be dismissed as
a fluke and often enough to page you at 3 a.m.

The demo widens the window on purpose so the failure is a repeatable experiment
rather than a lucky screenshot. **It widens the window; it does not create it.**

## Nothing repairs it

After the race, the two are watched for thirty seconds:

```
  t+ 0s  db=Berlin  cache=Toronto  agree=False  ttl=300s
  t+ 5s  db=Berlin  cache=Toronto  agree=False  ttl=295s
  t+15s  db=Berlin  cache=Toronto  agree=False  ttl=285s
  t+30s  db=Berlin  cache=Toronto  agree=False  ttl=270s
```

The only clock running is the TTL that was set *when the wrong value was
written*. Five minutes of confidently serving the wrong city.

---

## The knob

```bash
WRITE_POLICY=delete docker compose up --build
```

| `WRITE_POLICY` | What a write does | Races out of 20 that ended stale | Who ends up wrong |
| --- | --- | --- | --- |
| `update` *(default)* | write the DB, then write the cache | **20 / 20** | the cache |
| `delete` | write the DB, then delete the key | **0 / 20** | nobody |
| `write_through` | cache and DB written together | **20 / 20** | the database |
| `write_behind` | cache now, DB later | **20 / 20** | the database, *and* the write can be lost |

The walkthrough at <http://localhost:8080> switches between all four without a
restart, so you can watch the same race come out differently.

### Why deleting wins

Deleting does not make the cache correct. It makes it **unable to be wrong**.

Two writers racing to *set* a key leave the loser's value behind. Two writers
racing to *delete* a key both leave it deleted — and a deleted key is not a
wrong answer, it is a MISS, and a MISS is always answered by the database.

### Why write-through does not

Sold as the consistent one. At application level it is still two writes in some
order, so it races exactly like `update`; it just moves the wreckage. Measured
here, the *database* is the one left holding the losing writer's value — which
is worse, because now the durable copy is the wrong one.

### What write-behind costs

Writes get fast and Postgres gets a quiet life. Then the process dies with the
queue full:

```
  wrote city=Berlin. queued, not yet flushed:
    db=Lisbon    cache=Berlin    queue_depth=1
  killing the app before the flusher runs
  after restart:
    db=Lisbon    cache=Berlin    agree=False
```

The write is gone from Postgres. The cache is the only place it ever existed,
and it will evict it in five minutes.

---

## What correctness costs

Invalidation is not free. Deleting the key throws away a cached profile that
cost real work to build, so the next reader pays for it:

| | after a write | steady state |
| --- | --- | --- |
| `WRITE_POLICY=delete` | **22.29 ms** (MISS — back to Postgres) | 0.17 ms |
| `WRITE_POLICY=write_through` | **0.15 ms** (HIT — the key is already warm) | 0.15 ms |

A **22 ms** penalty on the first read after every write, in exchange for never
serving a stale answer. That is the actual trade, and it is why write-through
still has a job: it is not a consistency mechanism, it is a way to avoid the
miss.

For reference, this episode's own baseline on the same run: a cache HIT is
**0.14 ms**, the uncached read is **20.65 ms**.

> One number in `capture/metrics.json` looks like a contradiction and is not.
> The first read after a *quiet* write is 22.29 ms; the first read straight after
> a *contended* race is 42.17 ms. Invalidation costs most exactly when the system
> is busiest — which is the door Episode 3 walks through.

---

## What is in here

Episode 1's app, extended in place. Same schema, same endpoints, same
Cache-Aside read path. The only new surface is the write path.

```
app/writes.py     the four policies, one function each   ◀── the whole episode
app/main.py       + PUT /api/users/{id}, + /truth, + /reset
app/config.py     + WRITE_POLICY
scripts/race.py   the two-writer race, repeatable
```

`GET /api/users/42/truth` is the one to know: it reads Postgres and Redis
directly, side by side, and never repopulates — because the entire point is to
watch them disagree.

```bash
curl -s localhost:8000/api/users/42/truth
```

## Try this

Run the race under `update`, then set `CACHE_TTL_SECONDS=30` and run it again.
The bug does not go away; it just stops lasting five minutes. Then ask yourself
what TTL you would have to pick for that to be a fix — and notice that the
answer is "zero", which is another way of spelling "no cache".

## What this episode leaves broken

Correctness under *mutation* is solved. Nothing here survives hostile *traffic*.
A delete-on-write cache still lets five thousand simultaneous requests for one
missing key land on the database at once.

That is Episode 3.

---

MIT. Part of the [System Sense caching mini-series](../README.md).
