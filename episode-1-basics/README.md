# Episode 1 — The Basics: Why Caching Makes Systems Fast

Cache-Aside, demonstrated on a profile endpoint that is genuinely expensive to
compute, with the before-and-after latency measured on your own machine.

## Run it

```bash
docker compose up --build
```

Then open **<http://localhost:8080>**.

That is the whole setup. The page walks you through the experiment one button at
a time and shows the real numbers as you go — no curl, no second terminal,
nothing to install beyond Docker.

First start seeds 100,000 users and 120,000 orders. It takes a few seconds and
only happens once.

### What the walkthrough shows you

| Step | What happens |
| --- | --- |
| 1. Empty the cache | Deletes the `user:42` key, so the next read has to do real work. |
| 2. First read | **MISS** — aggregates 120,000 order rows in Postgres, then stores the result. |
| 3. Second read | **HIT** — same data, straight from memory, typically 100× faster. |
| 4. The control | An endpoint that never touches Redis. Press it repeatedly: it stays slow. |
| 5. The query plan | `EXPLAIN ANALYZE` for the read, showing why it was expensive to begin with. |

**Step 4 is the one worth pausing on.** A reasonable person will suspect the
second read was fast because *Postgres* warmed its own buffers, not because of
the cache. `/api/users/42/uncached` runs the identical query and never consults
Redis, so it stays slow no matter how often you call it. That is what turns the
comparison from a claim into evidence.

Below the walkthrough is a free-play panel: fire cached and uncached reads in any
order, burst 25 at once, clear the key, and watch the latency chart and the
running medians update.

---

## Prefer the terminal?

Everything the page does is two HTTP endpoints. Nothing is hidden.

```bash
# Cache-Aside: MISS the first time, HIT afterwards
curl -sD- -o/dev/null localhost:8000/api/users/42

# The control: always straight to Postgres, never touches Redis
curl -sD- -o/dev/null localhost:8000/api/users/42/uncached
```

`-sD- -o/dev/null` prints the response headers and throws away the JSON body,
which is what you want here — the interesting part is the headers:

```
x-cache: HIT
x-elapsed-ms: 0.17
x-cache-enabled: true
```

Just the numbers, if you prefer:

```bash
curl -sD- -o/dev/null localhost:8000/api/users/42 | grep -i '^x-'
```

The query plan, as plain text:

```bash
curl -s localhost:8000/api/users/42/explain
```

Clear the cached key and start over. This runs `redis-cli` inside the container,
so you do not need it installed:

```bash
docker compose exec redis redis-cli DEL user:42
```

---

## The experiment knob

One setting changes the behaviour of the whole app:

```bash
# Cache off — every read pays the full price, forever
CACHE_ENABLED=false docker compose up --build
```

The stack defaults to `CACHE_ENABLED=true` so the walkthrough works the moment it
starts. Run it with `false` and you get the world before caching: every read,
including the tenth identical one, is a MISS. The page notices and reports what
it is seeing.

**Then try this:** set `CACHE_TTL_SECONDS` to `10` in `docker-compose.yml`,
restart, and leave the page open. Watch the key expire and one unlucky request go
slow again every ten seconds. That is a cache stampede in miniature — and it is
what Episode 4 is about.

---

## Prove it without the UI

```bash
./scripts/capture-demo.sh
```

Tears the stack down, brings it up with the cache off, runs `EXPLAIN ANALYZE`,
benchmarks 30 requests, flips the knob, benchmarks 30 more, and writes
`capture/metrics.json`. Every number quoted in the video came out of this script,
and the logs are committed in `capture/` so you can compare a run on your own
hardware against the one in the episode.

---

## Where to look

| File | Why it matters |
| --- | --- |
| `app/cache.py` | **The entire pattern.** Ask the cache, fall back to the database, populate the cache. Fifteen lines. |
| `app/queries.py` | The SQL — including the aggregate that makes the read expensive enough to be worth caching. |
| `app/main.py` | Wiring: the pool, the client, the two endpoints. |
| `db/init.sql` | Seed data. |
| `demo/` | The walkthrough page. Not part of the lesson — it just drives the two endpoints above. |

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
