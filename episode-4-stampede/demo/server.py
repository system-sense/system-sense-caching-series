"""The demo UI's little backend.

It runs the episode's attacks against the app and reports what the app counted
while they were running. It never fabricates a number: every count below is read back
out of the application's own /metrics after the fact, and every latency is one
the application measured for a request it actually served.

The attacks here are deliberately small -- a few thousand requests, so a button
press finishes while you are looking at it. The full-sized versions are the k6
scripts in load-test/, and they tell the same story with two more zeroes:

    docker compose run --rm load-test run /scripts/stampede.js
"""
import asyncio
import os
import statistics
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_URL = os.getenv("APP_URL", "http://app:8000")
USER_ID = int(os.getenv("DEMO_USER_ID", "42"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")

# Ids past the 100,000 that exist. Nothing in this range was ever a user.
PHANTOM_LO = 1_000_000
PHANTOM_HI = 2_000_000

client = httpx.AsyncClient(timeout=120.0, limits=httpx.Limits(max_connections=300))


async def _announce() -> None:
    for _ in range(180):
        try:
            if (await client.get(f"{APP_URL}/health", timeout=2.0)).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    rule = "─" * 60
    print(
        f"\n{rule}\n"
        f"\n   System Sense · Episode 4 — The Boss Fight\n"
        f"\n   Open  {PUBLIC_URL}\n"
        f"\n{rule}\n",
        flush=True,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_announce())
    yield
    task.cancel()


app = FastAPI(title="Episode 4 demo", lifespan=lifespan)


# ── talking to the application ───────────────────────────────────────────────

async def _metrics() -> dict:
    return (await client.get(f"{APP_URL}/metrics")).json()


async def _health() -> dict:
    return (await client.get(f"{APP_URL}/health")).json()


async def _post(path: str, payload: dict) -> dict:
    return (await client.post(f"{APP_URL}{path}", json=payload)).json()


async def _arm() -> None:
    await client.post(f"{APP_URL}/metrics/reset")


async def _burst(ids, concurrency: int = 120) -> dict:
    """Fire every id at the app at once and record which layer answered.

    The layer comes from the X-Cache header the application set on that exact
    response, so 'the database saw this' is the app's own account of it.
    """
    sem = asyncio.Semaphore(concurrency)
    layers: dict[str, int] = {}
    lat: list[float] = []
    by_layer: dict[str, list] = {}
    lock = asyncio.Lock()

    async def one(uid: int) -> None:
        async with sem:
            try:
                r = await client.get(f"{APP_URL}/api/users/{uid}")
                layer = r.headers.get("X-Cache", "?")
                # The application's own measurement of that request, not the
                # wall time this page waited. Firing thousands of requests from
                # one container queues them here, and that queue is this page's
                # doing, not the cache's.
                ms = float(r.headers.get("X-Elapsed-Ms", 0))
            except (httpx.HTTPError, ValueError):
                layer, ms = "ERROR", 0.0
            async with lock:
                layers[layer] = layers.get(layer, 0) + 1
                lat.append(ms)
                by_layer.setdefault(layer, []).append(ms)

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in ids))
    elapsed = time.perf_counter() - started  # wall clock, for the request rate

    def summarise(values: list) -> dict | None:
        if not values:
            return None
        v = sorted(values)
        return {
            "n": len(v),
            "median": round(statistics.median(v), 2),
            "p95": round(v[min(len(v) - 1, int(0.95 * len(v)))], 2),
            "max": round(v[-1], 2),
        }

    s = sorted(lat)
    return {
        "requests": len(s),
        "seconds": round(elapsed, 2),
        "rps": round(len(s) / elapsed, 1) if elapsed else 0,
        "served_by": layers,
        "latency_ms": summarise(lat) or {"median": None, "p95": None, "max": None},
        # A run's overall median is meaningless when a handful of rebuilds hide
        # behind hundreds of hits, which is exactly the shape of Breakdown.
        "by_layer": {k: summarise(v) for k, v in by_layer.items()},
    }


# ── Failure 1: Penetration ───────────────────────────────────────────────────

@app.post("/api/attack/penetration")
async def penetration(body: dict = Body(default={})):
    """Ids that were never issued, with whichever defense you picked.

        mode=random   a different phantom id every time  (the real attack)
        mode=repeat   a small pool, reused               (the easy case)
    """
    import random

    defense = str(body.get("defense", "none"))
    mode = str(body.get("mode", "random"))
    n = min(int(body.get("requests", 2000)), 20000)
    pool = int(body.get("pool", 50))

    await _post("/api/defense", {"defense": defense, "jitter_seconds": 0})
    await _post("/api/cache/expire", {"all": True})
    await _arm()

    ids = [
        PHANTOM_LO + (random.randrange(pool) if mode == "repeat"
                      else random.randrange(PHANTOM_HI - PHANTOM_LO))
        for _ in range(n)
    ]
    result = await _burst(ids)
    m = await _metrics()
    c = m["counters"]

    return {
        "defense": defense,
        "mode": mode,
        **result,
        "db_queries": c["db_loads"],
        "db_queries_pct": round(100 * c["db_loads"] / max(c["requests"], 1), 1),
        "false_positives": c["false_positives"],
    }


# ── Failure 2: Avalanche ─────────────────────────────────────────────────────

@app.post("/api/attack/avalanche")
async def avalanche(body: dict = Body(default={})):
    """Warm a batch of keys, wait for the TTL, then send ordinary traffic.

    Run it once with jitter at 0 and once with jitter on. Same keys, same
    traffic, same waiting -- the only difference is one line of TTL arithmetic.
    """
    import random

    keys = min(int(body.get("keys", 2000)), 20000)
    ttl = max(int(body.get("ttl", 6)), 1)
    jitter = int(body.get("jitter", 0))
    n = min(int(body.get("requests", 2000)), 20000)

    await _post("/api/defense", {"defense": "none", "jitter_seconds": jitter})
    await _post("/api/cache/expire", {"all": True})
    warm = await _post("/api/cache/warm", {"count": keys, "ttl": ttl, "jitter": jitter})
    ttls = (await client.get(f"{APP_URL}/api/cache/ttls")).json()

    # Wait out the base TTL. Without jitter every key dies at this instant;
    # with jitter, only the unlucky few have.
    await asyncio.sleep(ttl + 0.5)
    await _arm()

    result = await _burst([1 + random.randrange(keys) for _ in range(n)])
    m = await _metrics()
    c = m["counters"]

    return {
        "jitter_s": jitter,
        "keys_warmed": warm.get("keys"),
        "ttl_base_s": ttl,
        "ttl_spread_s": ttls.get("spread_s"),
        "warm_ms": warm.get("total_ms"),
        **result,
        "db_queries": c["db_loads"],
        "db_queries_pct": round(100 * c["db_loads"] / max(c["requests"], 1), 1),
        "max_concurrent_db_loads": m["max_concurrent_db_loads"],
    }


# ── The boss fight: one key, two hundred readers, one expiry ─────────────────

async def _stream(uid: int, seconds: float, rps: int, concurrency: int = 200) -> dict:
    """Ordinary traffic at a steady rate, for long enough that the key expires
    underneath it more than once. A burst shows one stampede; this shows what
    living with the key is like."""
    layers: dict[str, int] = {}
    lat: list[float] = []
    by_layer: dict[str, list] = {}
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def one() -> None:
        async with sem:
            try:
                r = await client.get(f"{APP_URL}/api/users/{uid}")
                layer = r.headers.get("X-Cache", "?")
                ms = float(r.headers.get("X-Elapsed-Ms", 0))
            except (httpx.HTTPError, ValueError):
                layer, ms = "ERROR", 0.0
            async with lock:
                layers[layer] = layers.get(layer, 0) + 1
                lat.append(ms)
                by_layer.setdefault(layer, []).append(ms)

    started = time.perf_counter()
    tasks = []
    gap = 1.0 / max(rps, 1)
    while time.perf_counter() - started < seconds:
        tasks.append(asyncio.create_task(one()))
        await asyncio.sleep(gap)
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - started

    def summarise(values: list) -> dict | None:
        if not values:
            return None
        v = sorted(values)
        return {
            "n": len(v),
            "median": round(statistics.median(v), 2),
            "p95": round(v[min(len(v) - 1, int(0.95 * len(v)))], 2),
            "max": round(v[-1], 2),
        }

    return {
        "requests": len(lat),
        "seconds": round(elapsed, 2),
        "rps": round(len(lat) / elapsed, 1) if elapsed else 0,
        "served_by": layers,
        "latency_ms": summarise(lat) or {"median": None, "p95": None, "max": None},
        "by_layer": {k: summarise(v) for k, v in by_layer.items()},
    }


async def _configure(**kw) -> dict:
    return await _post("/api/stampede", kw)


@app.post("/api/attack/stampede")
async def stampede(body: dict = Body(default={})):
    """The event Episode 3 ended on, with and without the lock.

        defense=none   every reader that missed ran the same aggregate
        defense=lock   one reader ran it; the rest waited and read the cache

    XFetch is not on this button on purpose: this experiment *deletes* the key,
    and a deleted key is gone whatever arithmetic you were doing about its TTL.
    XFetch is the next panel, where the key is allowed to expire on its own.
    """
    n = min(int(body.get("requests", 200)), 2000)
    uid = int(body.get("uid", USER_ID))
    defense = str(body.get("defense", "none"))

    await _configure(stampede=defense, lock_ttl_ms=5000, lock_release="lua",
                     cache_ttl_seconds=300)
    await _post("/api/cache/expire", {"all": True})
    await client.get(f"{APP_URL}/api/users/{uid}")          # warm the one key
    await _arm()

    # Everyone is already reading it happily...
    warm_phase = await _burst([uid] * min(n, 200), concurrency=100)
    # ...and now it expires.
    await _post("/api/cache/expire", {"uid": uid})
    cold_phase = await _burst([uid] * n, concurrency=min(n, 400))

    m = await _metrics()
    c = m["counters"]
    hottest = next((h for h in m["hottest_keys"] if h["uid"] == uid), None)
    queries = hottest["db_loads"] if hottest else 0

    return {
        "uid": uid,
        "defense": defense,
        "while_cached": warm_phase,
        "after_expiry": cold_phase,
        "hit_ms": (cold_phase["by_layer"].get("HIT") or {}).get("median"),
        "rebuild_ms": (cold_phase["by_layer"].get("MISS")
                       or cold_phase["by_layer"].get("LEADER") or {}).get("median"),
        "wait_ms": (cold_phase["by_layer"].get("WAIT_HIT") or {}).get("median"),
        "slowest_ms": cold_phase["latency_ms"]["max"],
        "db_queries_for_one_key": queries,
        "max_concurrent_db_loads": m["max_concurrent_db_loads"],
        "max_waiting_on_lock": m["max_waiting_on_lock"],
        "wasted_queries": max(queries - 1, 0),
        "waited": c["wait_hits"],
    }


@app.post("/api/attack/sustained")
async def sustained(body: dict = Body(default={})):
    """The same key, left to expire on its own, over and over.

        defense=none     every expiry is a cliff
        defense=lock     one rebuild per expiry; the rest wait for it
        defense=xfetch   the key is rebuilt before it expires. Nobody waits.
    """
    uid = int(body.get("uid", USER_ID))
    defense = str(body.get("defense", "none"))
    ttl = max(int(body.get("ttl", 3)), 1)
    seconds = min(float(body.get("seconds", 9)), 30)
    rps = min(int(body.get("rps", 150)), 1000)

    await _configure(stampede=defense, lock_ttl_ms=5000, lock_release="lua",
                     cache_ttl_seconds=ttl)
    await _post("/api/cache/expire", {"all": True})
    await client.get(f"{APP_URL}/api/users/{uid}")   # warm, and write the metadata
    await _arm()

    result = await _stream(uid, seconds, rps)
    m = await _metrics()
    c = m["counters"]

    return {
        "defense": defense,
        "ttl_s": ttl,
        "expiries_expected": int(seconds // ttl),
        **result,
        "db_queries": c["db_loads"],
        "early_refreshes": c["early_refreshes"],
        "waited": c["wait_hits"],
        "max_waiting_on_lock": m["max_waiting_on_lock"],
        "max_concurrent_db_loads": m["max_concurrent_db_loads"],
    }


@app.post("/api/attack/release")
async def release(body: dict = Body(default={})):
    """The bug most tutorials skip.

    Hold the lock for less time than the rebuild takes, and it expires while
    its holder is still working. With a plain DEL that holder then deletes the
    lock that replaced it. With the Lua compare-and-delete it cannot.
    """
    uid = int(body.get("uid", USER_ID))
    mode = str(body.get("release", "unsafe"))
    lock_ttl_ms = int(body.get("lock_ttl_ms", 10))
    ttl = max(int(body.get("ttl", 2)), 1)
    seconds = min(float(body.get("seconds", 8)), 30)
    rps = min(int(body.get("rps", 300)), 1000)

    await _configure(stampede="lock", lock_ttl_ms=lock_ttl_ms, lock_release=mode,
                     cache_ttl_seconds=ttl)
    await _post("/api/cache/expire", {"all": True})
    await client.get(f"{APP_URL}/api/users/{uid}")
    await _arm()

    result = await _stream(uid, seconds, rps)
    m = await _metrics()
    c = m["counters"]

    return {
        "release": mode,
        "lock_ttl_ms": lock_ttl_ms,
        "ttl_s": ttl,
        **result,
        "rebuilds": c["db_loads"],
        "wrongful": c["release_wrongful"],
        "refused": c["release_refused"],
        "gone": c["release_gone"],
        "own": c["released_own"],
        "max_concurrent_db_loads": m["max_concurrent_db_loads"],
    }


# ── state, for the page ──────────────────────────────────────────────────────

@app.get("/api/state")
async def state():
    try:
        h = await _health()
        m = await _metrics()
    except httpx.HTTPError:
        return JSONResponse({"error": "app unreachable"}, status_code=502)
    return {
        "defense": h["defense"],
        "stampede": h["stampede"],
        "lock_ttl_ms": h["lock_ttl_ms"],
        "lock_release": h["lock_release"],
        "xfetch_beta": h["xfetch_beta"],
        "ttl_jitter_seconds": h["ttl_jitter_seconds"],
        "negative_ttl_seconds": h["negative_ttl_seconds"],
        "cache_ttl_seconds": h["cache_ttl_seconds"],
        "write_policy": h["write_policy"],
        "bloom": h["bloom"],
        "bloom_build_ms": h["bloom_build_ms"],
        "redis_keys": m.get("redis_keys"),
        "user_id": USER_ID,
    }


@app.get("/api/series")
async def series():
    return (await client.get(f"{APP_URL}/metrics/series")).json()


@app.post("/api/reset")
async def reset():
    await _post("/api/defense", {"defense": "none", "jitter_seconds": 0})
    await _configure(stampede="none", lock_ttl_ms=5000, lock_release="lua",
                     cache_ttl_seconds=300)
    await _post("/api/cache/expire", {"all": True})
    await _arm()
    return await state()


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
