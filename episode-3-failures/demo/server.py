"""The demo UI's little backend.

It runs three attacks against the app and reports what the app counted while
they were running. It never fabricates a number: every count below is read back
out of the application's own /metrics after the fact, and every latency is one
the application measured for a request it actually served.

The attacks here are deliberately small -- a few thousand requests, so a button
press finishes while you are looking at it. The full-sized versions are the k6
scripts in load-test/, and they tell the same story with two more zeroes:

    docker compose run --rm load-test run /scripts/penetration.js
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
        f"\n   System Sense · Episode 3 — The 3 Classic Failures\n"
        f"\n   Open  {PUBLIC_URL}\n"
        f"\n{rule}\n",
        flush=True,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_announce())
    yield
    task.cancel()


app = FastAPI(title="Episode 3 demo", lifespan=lifespan)


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


# ── Failure 3: Breakdown -- no defense to pick. That is the point. ───────────

@app.post("/api/attack/breakdown")
async def breakdown(body: dict = Body(default={})):
    """One key, many readers, and the key expires underneath them.

    There is no toggle on this one. Nothing in Cache-Aside says "somebody else
    is already fetching this", so every reader in flight fetches it too.
    """
    n = min(int(body.get("requests", 400)), 5000)
    uid = int(body.get("uid", USER_ID))

    await _post("/api/defense", {"defense": "none", "jitter_seconds": 0})
    await _post("/api/cache/expire", {"all": True})
    await client.get(f"{APP_URL}/api/users/{uid}")          # warm the one key
    await _arm()

    # Everyone is already reading it happily...
    warm_phase = await _burst([uid] * min(n, 300), concurrency=120)
    # ...and now it expires.
    await _post("/api/cache/expire", {"uid": uid})
    cold_phase = await _burst([uid] * n, concurrency=min(n, 300))

    m = await _metrics()
    hottest = next((h for h in m["hottest_keys"] if h["uid"] == uid), None)

    return {
        "uid": uid,
        "while_cached": warm_phase,
        "after_expiry": cold_phase,
        "hit_ms": (cold_phase["by_layer"].get("HIT") or {}).get("median"),
        "rebuild_ms": (cold_phase["by_layer"].get("MISS") or {}).get("median"),
        "slowest_rebuild_ms": (cold_phase["by_layer"].get("MISS") or {}).get("max"),
        "db_queries_for_one_key": hottest["db_loads"] if hottest else 0,
        "max_concurrent_db_loads": m["max_concurrent_db_loads"],
        "wasted_queries": max((hottest["db_loads"] if hottest else 1) - 1, 0),
        "fix": "Episode 4",
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
    await _post("/api/cache/expire", {"all": True})
    await _arm()
    return await state()


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
