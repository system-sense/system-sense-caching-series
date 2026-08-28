# Episode 1 — The Basics: Why Caching Makes Systems Fast

Cache-Aside, demonstrated on a profile endpoint that is genuinely expensive to
compute, with the before-and-after latency printed in your own terminal.

## Run it

```bash
docker compose up --build
```

First start seeds 100,000 users and 300,000 orders; that takes a few seconds
and only happens once. Then:

```bash
curl -i localhost:8000/api/users/42
```

Look at the `X-Cache` and `X-Elapsed-Ms` response headers.

## The experiment knob

One setting changes the behaviour of the whole app:

```bash
# Cache off — every read hits Postgres
CACHE_ENABLED=false docker compose up --build

# Cache on — the same reads come from Redis
CACHE_ENABLED=true docker compose up --build
```

**What to expect:** with the cache off, every request pays the full profile
aggregate. With it on, the first request still pays it (that is the MISS that
populates the key) and every request after that is served from memory. Watch
`X-Elapsed-Ms` collapse between request one and request two.

Then try changing `CACHE_TTL_SECONDS` in `docker-compose.yml` to `10`, and
watch a request go slow again every ten seconds as the key expires.

## Prove it for yourself

```bash
./scripts/capture-demo.sh
```

Tears the stack down, brings it up with the cache off, runs `EXPLAIN ANALYZE`,
benchmarks 30 requests, flips the knob, benchmarks 30 more, and writes
`capture/metrics.json`. Every number quoted in the video came out of this
script.

## Where to look

| File | Why it matters |
| --- | --- |
| `app/cache.py` | **The entire pattern.** Ask the cache, fall back to the database, populate the cache. |
| `app/queries.py` | The SQL — including the aggregate that makes the read expensive enough to be worth caching. |
| `app/main.py` | Wiring: the pool, the client, the endpoint. |
| `db/init.sql` | Seed data. |

## Why the endpoint is not `SELECT * FROM users WHERE id = 42`

Because that query takes about 0.2 ms on an indexed primary key, and caching it
would prove nothing. A real profile is a *composite* read: who the user is,
what they have spent, how that ranks against every other customer, and what
they bought recently. The ranking aggregates the whole orders table — expensive
to compute, cheap to store, slow to change. That is the shape of data worth
caching, and it is why the numbers in this demo are worth looking at.

## What this episode deliberately leaves broken

The write path. Nothing here invalidates anything. Update a user row and the
cache will happily keep serving the old copy until the TTL expires.

That is Episode 2.
