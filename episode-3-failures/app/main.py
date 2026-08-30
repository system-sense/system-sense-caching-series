"""System Sense -- Episode 3: The 3 Classic Cache Failures.

Episode 1's read path and Episode 2's write path are below, unchanged. Episode 2
concluded that `delete` is the write policy that does not go stale, so that is
the default and this episode stops arguing about writes.

What is new is everything needed to attack this app with traffic and prove what
happened afterwards: counters, a four-times-a-second sample of the whole thing,
a batch warmer, and the two defenses.

Run it:        docker compose up --build
Attack it:     docker compose run --rm load-test run /scripts/penetration.js
Watch it:      curl -s localhost:8000/metrics
The knobs:     PENETRATION_DEFENSE=bloom  TTL_JITTER_SECONDS=300
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import Body, FastAPI, Response
from fastapi.responses import JSONResponse

from . import config, metrics, writes
from .bloom import BloomFilter
from .cache import TOMBSTONE, get_profile, ttl_seconds
from .queries import (
    ALL_USER_IDS,
    BATCH_PROFILES,
    BATCH_RECENT_ORDERS,
    MAX_USER_ID,
    PROFILE_STATS,
    RECENT_ORDERS,
    RESET_USER_CITY,
    SELECT_USER,
    UPDATE_USER_CITY,
)

state: dict = {}
flush_counter = {"flushed": 0}


async def load_profile(uid: int, db):
    """The real read. Three queries, one of them genuinely expensive.

    This is the *only* function in the app that reaches Postgres for a profile,
    which is what makes `db_loads` a count rather than an estimate. Every
    failure in this episode is measured by how many times this runs.
    """
    metrics.enter_load(uid)
    try:
        metrics.bump("db_loads")
        async with db.acquire() as con:
            user = await con.fetchrow(SELECT_USER, uid)
            if user is None:
                return None
            stats = await con.fetchrow(PROFILE_STATS, uid)
            recent = await con.fetch(RECENT_ORDERS, uid)
    finally:
        metrics.exit_load()

    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "city": user["city"],
        "joined_at": user["joined_at"].isoformat(),
        "order_count": stats["order_count"] if stats else 0,
        "lifetime_cents": int(stats["lifetime_cents"]) if stats else 0,
        "spend_percentile": float(stats["spend_percentile"]) if stats else 0.0,
        "recent_orders": [
            {
                "id": r["id"],
                "item": r["item"],
                "amount_cents": r["amount_cents"],
                "placed_at": r["placed_at"].isoformat(),
            }
            for r in recent
        ],
    }


async def build_bloom(db) -> BloomFilter:
    """Every id that exists, in about 117 KB.

    Rebuilt at startup. A production filter is rebuilt on a schedule or fed by
    the write path; the interesting property -- that it never says "no" about a
    row that exists -- is the same either way.
    """
    started = time.perf_counter()
    async with db.acquire() as con:
        row = await con.fetchrow(MAX_USER_ID)
        ids = await con.fetch(ALL_USER_IDS)

    bf = BloomFilter(expected=row["n"], fp_rate=config.BLOOM_FP_RATE)
    for r in ids:
        bf.add(r["id"])

    state["bloom_build_ms"] = round((time.perf_counter() - started) * 1000, 1)
    state["max_user_id"] = row["max_id"]
    print(
        f"[bloom] {bf.n} ids, {bf.size_bytes} bytes, k={bf.k}, "
        f"built in {state['bloom_build_ms']} ms",
        flush=True,
    )
    return bf


async def sampler() -> None:
    while True:
        await asyncio.sleep(metrics.SAMPLE_MS / 1000)
        metrics.sample()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["db"] = await asyncpg.create_pool(config.DATABASE_URL, min_size=4, max_size=20)
    state["redis"] = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    state["queue"] = asyncio.Queue()
    state["bloom"] = await build_bloom(state["db"])
    tasks = [
        asyncio.create_task(
            writes.flusher(state["queue"], config.WRITE_BEHIND_FLUSH_MS, flush_counter)
        ),
        asyncio.create_task(sampler()),
    ]
    print(
        f"[startup] cache_enabled={config.CACHE_ENABLED} ttl={config.CACHE_TTL_SECONDS}s "
        f"write_policy={config.write_policy()} defense={config.defense()} "
        f"ttl_jitter={config.jitter_seconds()}s",
        flush=True,
    )
    yield
    for t in tasks:
        t.cancel()
    await state["db"].close()
    await state["redis"].aclose()


app = FastAPI(title="System Sense -- Episode 3", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "cache_enabled": config.CACHE_ENABLED,
        "cache_ttl_seconds": config.CACHE_TTL_SECONDS,
        "write_policy": config.write_policy(),
        "defense": config.defense(),
        "ttl_jitter_seconds": config.jitter_seconds(),
        "negative_ttl_seconds": config.NEGATIVE_TTL_SECONDS,
        "bloom": state["bloom"].describe() if "bloom" in state else None,
        "bloom_build_ms": state.get("bloom_build_ms"),
        "max_user_id": state.get("max_user_id"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Episode 1 -- the read path
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/users/{uid}")
async def read_user(uid: int):
    started = time.perf_counter()
    profile, status = await get_profile(
        uid, state["redis"], state["db"], load_profile, state.get("bloom")
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    headers = {
        "X-Cache": status,
        "X-Elapsed-Ms": f"{elapsed_ms:.2f}",
        "X-Defense": config.defense(),
    }

    if profile is None:
        # A 404 is a normal answer here, not an error -- half this episode is
        # about ids that do not exist. The header says which layer answered.
        return JSONResponse(
            {"detail": "user not found"}, status_code=404, headers=headers
        )

    return Response(
        content=json.dumps(profile), media_type="application/json", headers=headers
    )


@app.get("/api/users/{uid}/uncached")
async def read_user_uncached(uid: int):
    """The same read, always straight to Postgres. Never touches Redis."""
    started = time.perf_counter()
    profile = await load_profile(uid, state["db"])
    elapsed_ms = (time.perf_counter() - started) * 1000

    if profile is None:
        return JSONResponse({"detail": "user not found"}, status_code=404)

    return Response(
        content=json.dumps(profile),
        media_type="application/json",
        headers={"X-Cache": "BYPASS", "X-Elapsed-Ms": f"{elapsed_ms:.2f}"},
    )


@app.get("/api/users/{uid}/explain")
async def explain_user(uid: int):
    """EXPLAIN ANALYZE for the expensive query, as plain text."""
    async with state["db"].acquire() as con:
        rows = await con.fetch(f"EXPLAIN ANALYZE {PROFILE_STATS}", uid)
    return Response("\n".join(r[0] for r in rows), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════════
#  Episode 2 -- the write path, settled. `delete` by default.
# ═══════════════════════════════════════════════════════════════════════════


@app.put("/api/users/{uid}")
async def write_user(uid: int, body: dict = Body(...)):
    city = body.get("city")
    stall_ms = int(body.get("stall_ms", 0))
    if not city:
        return JSONResponse({"detail": "city required"}, status_code=400)
    if config.write_policy() not in config.POLICIES:
        return JSONResponse(
            {"detail": f"unknown WRITE_POLICY {config.write_policy()!r}"}, status_code=500
        )

    key = f"user:{uid}"
    started = time.perf_counter()

    async def apply_db():
        async with state["db"].acquire() as con:
            await con.execute(UPDATE_USER_CITY, uid, city)

    async def rebuild(pending: bool = False):
        profile = await load_profile(uid, state["db"])
        if profile is None:
            return None
        return {**profile, "city": city} if pending else profile

    policy = config.write_policy()
    if policy == "update":
        action = await writes.update(uid, key, apply_db, rebuild, state["redis"], stall_ms)
    elif policy == "delete":
        action = await writes.delete(uid, key, apply_db, rebuild, state["redis"], stall_ms)
    elif policy == "write_through":
        action = await writes.write_through(
            uid, key, apply_db, rebuild, state["redis"], stall_ms
        )
    else:
        action = await writes.write_behind(
            uid, key, apply_db, rebuild, state["redis"], stall_ms, state["queue"]
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    return Response(
        content=json.dumps({"city": city, "policy": policy, "action": action}),
        media_type="application/json",
        headers={
            "X-Write-Policy": policy,
            "X-Cache-Action": action,
            "X-Elapsed-Ms": f"{elapsed_ms:.2f}",
        },
    )


@app.get("/api/users/{uid}/truth")
async def truth(uid: int):
    """What Postgres says, and what Redis says, side by side."""
    async with state["db"].acquire() as con:
        row = await con.fetchrow(SELECT_USER, uid)

    key = f"user:{uid}"
    raw = await state["redis"].get(key)
    ttl = await state["redis"].ttl(key)
    cached = json.loads(raw) if raw and raw != TOMBSTONE else None

    return {
        "user_id": uid,
        "db_city": row["city"] if row else None,
        "cache_city": cached["city"] if cached else None,
        "cached": cached is not None,
        "tombstone": raw == TOMBSTONE,
        "ttl": ttl if ttl and ttl >= 0 else None,
        "agree": cached is None or (row is not None and row["city"] == cached["city"]),
        "policy": config.write_policy(),
        "queue_depth": state["queue"].qsize(),
    }


@app.post("/api/users/{uid}/reset")
async def reset(uid: int, body: dict = Body(default={})):
    city = body.get("city", "Lisbon")
    async with state["db"].acquire() as con:
        await con.execute(RESET_USER_CITY, uid, city)
    await state["redis"].delete(f"user:{uid}")
    return {"reset_to": city, "key_deleted": True}


@app.post("/api/policy")
async def set_policy(body: dict = Body(...)):
    p = str(body.get("policy", "")).strip().lower()
    if p not in config.POLICIES:
        return JSONResponse(
            {"detail": f"policy must be one of {config.POLICIES}"}, status_code=400
        )
    config.set_write_policy(p)
    return {"write_policy": p}


# ═══════════════════════════════════════════════════════════════════════════
#  Episode 3 -- the defenses, the warmer, and the evidence
# ═══════════════════════════════════════════════════════════════════════════


@app.post("/api/defense")
async def set_defense(body: dict = Body(...)):
    """Turn a defense on or off mid-attack, no restart.

    The documented knobs are the PENETRATION_DEFENSE and TTL_JITTER_SECONDS
    environment variables. This exists so the walkthrough can flip one while
    the load test is running and the curve can be watched changing shape.
    """
    if "defense" in body:
        d = str(body["defense"]).strip().lower()
        if d not in config.DEFENSES:
            return JSONResponse(
                {"detail": f"defense must be one of {config.DEFENSES}"}, status_code=400
            )
        config.set_defense(d)
        metrics.mark(f"defense={d}")

    if "jitter_seconds" in body:
        config.set_jitter_seconds(int(body["jitter_seconds"]))
        metrics.mark(f"jitter={config.jitter_seconds()}s")

    return {"defense": config.defense(), "jitter_seconds": config.jitter_seconds()}


@app.post("/api/cache/warm")
async def warm(body: dict = Body(default={})):
    """The midnight batch job.

    Aggregates once, then writes `count` keys in a single Redis pipeline. With
    jitter at 0 every one of them carries the same TTL and therefore the same
    expiry instant -- which is the Avalanche, set up in one call.
    """
    count = int(body.get("count", 5000))
    ttl = int(body.get("ttl", config.CACHE_TTL_SECONDS))
    jitter = int(body.get("jitter", config.jitter_seconds()))

    started = time.perf_counter()
    async with state["db"].acquire() as con:
        profiles = await con.fetch(BATCH_PROFILES, count)
        orders = await con.fetch(BATCH_RECENT_ORDERS, count)
    query_ms = (time.perf_counter() - started) * 1000

    by_user: dict[int, list] = {}
    for o in orders:
        by_user.setdefault(o["user_id"], []).append(
            {
                "id": o["id"],
                "item": o["item"],
                "amount_cents": o["amount_cents"],
                "placed_at": o["placed_at"].isoformat(),
            }
        )

    import random as _random

    ttls = []
    pipe = state["redis"].pipeline()
    for p in profiles:
        t = ttl + (_random.randint(0, jitter) if jitter > 0 else 0)
        ttls.append(t)
        pipe.setex(
            f"user:{p['id']}",
            t,
            json.dumps(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "email": p["email"],
                    "city": p["city"],
                    "joined_at": p["joined_at"].isoformat(),
                    "order_count": p["order_count"],
                    "lifetime_cents": int(p["lifetime_cents"]),
                    "spend_percentile": float(p["spend_percentile"]),
                    "recent_orders": by_user.get(p["id"], []),
                }
            ),
        )
    await pipe.execute()
    elapsed_ms = (time.perf_counter() - started) * 1000

    metrics.mark(f"warmed {len(ttls)} keys ttl={ttl}+rand(0,{jitter})")
    print(
        f"[warm] {len(ttls)} keys  ttl={ttl}s jitter={jitter}s  "
        f"query={query_ms:.0f} ms  total={elapsed_ms:.0f} ms",
        flush=True,
    )

    return {
        "keys": len(ttls),
        "ttl_base_s": ttl,
        "jitter_s": jitter,
        "ttl_min_s": min(ttls) if ttls else None,
        "ttl_max_s": max(ttls) if ttls else None,
        # One aggregate for every key, which is the point: a batch warmer is
        # cheap. It is the moment they all expire together that is expensive.
        "aggregate_query_ms": round(query_ms, 1),
        "total_ms": round(elapsed_ms, 1),
    }


@app.get("/api/cache/ttls")
async def ttls(sample: int = 2000, buckets: int = 20):
    """The expiry histogram -- the Avalanche, as a shape.

    Every warmed key's remaining TTL, bucketed. Without jitter this is one bar.
    """
    keys = []
    async for k in state["redis"].scan_iter(match="user:*", count=1000):
        keys.append(k)
        if len(keys) >= sample:
            break
    if not keys:
        return {"keys": 0, "buckets": []}

    pipe = state["redis"].pipeline()
    for k in keys:
        pipe.ttl(k)
    values = [v for v in await pipe.execute() if isinstance(v, int) and v >= 0]
    if not values:
        return {"keys": 0, "buckets": []}

    lo, hi = min(values), max(values)
    width = max(1, (hi - lo + 1) / buckets)
    hist = [0] * buckets
    for v in values:
        hist[min(buckets - 1, int((v - lo) / width))] += 1

    return {
        "keys": len(values),
        "ttl_min_s": lo,
        "ttl_max_s": hi,
        "spread_s": hi - lo,
        "bucket_width_s": round(width, 2),
        "buckets": [
            {"from_s": round(lo + i * width, 1), "keys": n} for i, n in enumerate(hist)
        ],
    }


@app.post("/api/cache/expire")
async def expire(body: dict = Body(default={})):
    """Expire keys on demand, so the moment is reproducible.

    A hot key that expires at an unpredictable moment is a production incident.
    Expiring it on command is the same event, on a laptop, at a chosen second.
    """
    if body.get("all"):
        deleted = 0
        keys: list[str] = []
        async for k in state["redis"].scan_iter(match="user:*", count=1000):
            keys.append(k)
            if len(keys) >= 5000:
                deleted += await state["redis"].delete(*keys)
                keys = []
        if keys:
            deleted += await state["redis"].delete(*keys)
        metrics.mark(f"expired all ({deleted} keys)")
        return {"deleted": deleted}

    uid = int(body.get("uid", 42))
    deleted = await state["redis"].delete(f"user:{uid}")
    metrics.mark(f"expired user:{uid}")
    return {"deleted": deleted, "uid": uid}


@app.get("/metrics")
async def read_metrics():
    snap = metrics.snapshot()
    snap["config"] = {
        "defense": config.defense(),
        "ttl_jitter_seconds": config.jitter_seconds(),
        "cache_ttl_seconds": config.CACHE_TTL_SECONDS,
        "negative_ttl_seconds": config.NEGATIVE_TTL_SECONDS,
    }
    snap["redis_keys"] = await state["redis"].dbsize()
    return snap


@app.get("/metrics/series")
async def read_series():
    return metrics.timeseries()


@app.post("/metrics/reset")
async def reset_metrics():
    metrics.reset()
    return {"reset": True}


@app.post("/metrics/mark")
async def add_mark(body: dict = Body(default={})):
    metrics.mark(str(body.get("label", "mark")))
    return {"marked": True}


@app.post("/api/pg/reset")
async def pg_reset():
    """Zero Postgres' own view, so the count that follows is this run's."""
    async with state["db"].acquire() as con:
        await con.execute("SELECT pg_stat_statements_reset()")
    return {"reset": True}


@app.get("/api/pg/stats")
async def pg_stats(limit: int = 6):
    """What Postgres says it executed -- independent of anything the app counted.

    Two numbers that disagree would mean the app is wrong about itself. They do
    not disagree, which is why both are in the capture.
    """
    async with state["db"].acquire() as con:
        rows = await con.fetch(
            """
            SELECT calls,
                   round(total_exec_time::numeric, 1) AS total_ms,
                   round(mean_exec_time::numeric, 3)  AS mean_ms,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 70) AS query
            FROM   pg_stat_statements
            WHERE  query NOT ILIKE '%pg_stat_statements%'
            ORDER  BY calls DESC
            LIMIT  $1
            """,
            limit,
        )
    return {
        "top": [dict(r) for r in rows],
        "total_calls": sum(r["calls"] for r in rows),
    }
