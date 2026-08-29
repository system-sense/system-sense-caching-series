"""System Sense -- Episode 2: The Invalidation Nightmare.

Episode 1's read path is below, unchanged. Everything new is the write path.

Run it:      docker compose up --build
Read it:     curl -i localhost:8000/api/users/42
Write it:    curl -X PUT localhost:8000/api/users/42 -d '{"city":"Berlin"}'
Compare:     curl -s localhost:8000/api/users/42/truth
The knob:    WRITE_POLICY=delete docker compose up --build
"""
import json
import time
from contextlib import asynccontextmanager

import asyncio

import asyncpg
import redis.asyncio as aioredis
from fastapi import Body, FastAPI, Response
from fastapi.responses import JSONResponse

from . import config, writes
from .cache import get_profile
from .queries import (
    PROFILE_STATS,
    RECENT_ORDERS,
    RESET_USER_CITY,
    SELECT_USER,
    UPDATE_USER_CITY,
)

state: dict = {}
flush_counter = {"flushed": 0}


async def load_profile(uid: int, db):
    """The real read. Three queries, one of them genuinely expensive."""
    async with db.acquire() as con:
        user = await con.fetchrow(SELECT_USER, uid)
        if user is None:
            return None
        stats = await con.fetchrow(PROFILE_STATS, uid)
        recent = await con.fetch(RECENT_ORDERS, uid)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["db"] = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
    state["redis"] = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    state["queue"] = asyncio.Queue()
    task = asyncio.create_task(
        writes.flusher(state["queue"], config.WRITE_BEHIND_FLUSH_MS, flush_counter)
    )
    print(
        f"[startup] cache_enabled={config.CACHE_ENABLED} "
        f"ttl={config.CACHE_TTL_SECONDS}s write_policy={config.write_policy()}",
        flush=True,
    )
    yield
    task.cancel()
    await state["db"].close()
    await state["redis"].aclose()


app = FastAPI(title="System Sense -- Episode 2", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "cache_enabled": config.CACHE_ENABLED,
        "write_policy": config.write_policy(),
        "queue_depth": state["queue"].qsize() if "queue" in state else 0,
        "flushed": flush_counter["flushed"],
    }


@app.get("/api/users/{uid}")
async def read_user(uid: int):
    started = time.perf_counter()
    profile, status = await get_profile(uid, state["redis"], state["db"], load_profile)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if profile is None:
        return JSONResponse({"detail": "user not found"}, status_code=404)

    print(f"GET /api/users/{uid}  {status:<4}  {elapsed_ms:7.2f} ms", flush=True)

    return Response(
        content=json.dumps(profile),
        media_type="application/json",
        headers={
            "X-Cache": status,
            "X-Elapsed-Ms": f"{elapsed_ms:.2f}",
            "X-Cache-Enabled": str(config.CACHE_ENABLED).lower(),
        },
    )


@app.get("/api/users/{uid}/uncached")
async def read_user_uncached(uid: int):
    """The same read, always straight to Postgres. Never touches Redis.

    This is the control. Without it, someone can reasonably object that the
    second request was fast because *Postgres* had warmed its buffers, not
    because of the cache. Hit this endpoint as often as you like: it stays
    slow, which is what makes the comparison an argument rather than a claim.
    """
    started = time.perf_counter()
    profile = await load_profile(uid, state["db"])
    elapsed_ms = (time.perf_counter() - started) * 1000

    if profile is None:
        return JSONResponse({"detail": "user not found"}, status_code=404)

    print(f"GET /api/users/{uid}/uncached  BYPASS  {elapsed_ms:7.2f} ms", flush=True)

    return Response(
        content=json.dumps(profile),
        media_type="application/json",
        headers={
            "X-Cache": "BYPASS",
            "X-Elapsed-Ms": f"{elapsed_ms:.2f}",
            "X-Cache-Enabled": str(config.CACHE_ENABLED).lower(),
        },
    )


@app.get("/api/users/{uid}/explain")
async def explain_user(uid: int):
    """EXPLAIN ANALYZE for the expensive query, as plain text."""
    async with state["db"].acquire() as con:
        rows = await con.fetch(f"EXPLAIN ANALYZE {PROFILE_STATS}", uid)
    return Response("\n".join(r[0] for r in rows), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════════
#  Episode 2 -- the write path
# ═══════════════════════════════════════════════════════════════════════════


@app.put("/api/users/{uid}")
async def write_user(uid: int, body: dict = Body(...)):
    """Change one field, then tell the cache about it -- somehow.

    `city` is deliberately a cheap column. Writing it costs Postgres well under
    a millisecond, and it throws away a cached profile that cost thirty-odd
    milliseconds to aggregate. Real caches hold coarse objects; real writes are
    narrow. That mismatch is why this is hard.

    `stall_ms` widens the window between the two writes. See app/writes.py.
    """
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
        # write-through and write-behind put the value in the cache BEFORE
        # Postgres has it, so the snapshot has to carry the pending change.
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
    print(
        f"PUT /api/users/{uid}  {policy:<13} {action:<11} city={city:<9} "
        f"stall={stall_ms:>4}ms  {elapsed_ms:7.2f} ms",
        flush=True,
    )

    return Response(
        content=json.dumps({"city": city, "policy": policy, "action": action}),
        media_type="application/json",
        headers={
            "X-Write-Policy": policy,
            "X-Cache-Action": action,
            "X-Elapsed-Ms": f"{elapsed_ms:.2f}",
            # Writes are timed with the stall removed as well, because the
            # stall is the demo's doing, not the policy's.
            "X-Elapsed-Net-Ms": f"{max(elapsed_ms - stall_ms, 0.0):.2f}",
        },
    )


@app.get("/api/users/{uid}/truth")
async def truth(uid: int):
    """What Postgres says, and what Redis says, side by side.

    Reads the cached key directly. It never repopulates and never falls back,
    because the entire point is to see the two disagree.
    """
    async with state["db"].acquire() as con:
        row = await con.fetchrow(SELECT_USER, uid)

    key = f"user:{uid}"
    raw = await state["redis"].get(key)
    ttl = await state["redis"].ttl(key)
    cached = json.loads(raw) if raw else None

    db_city = row["city"] if row else None
    cache_city = cached["city"] if cached else None

    return {
        "user_id": uid,
        "db_city": db_city,
        "cache_city": cache_city,
        "cached": cached is not None,
        "ttl": ttl if ttl and ttl >= 0 else None,
        # A missing key is not a disagreement. It is a MISS, and a MISS is
        # always answered by the database.
        "agree": cached is None or db_city == cache_city,
        "policy": config.write_policy(),
        "queue_depth": state["queue"].qsize(),
    }


@app.post("/api/users/{uid}/reset")
async def reset(uid: int, body: dict = Body(default={})):
    """Put the user back to a known city and drop the key, so the race can run
    again from an identical starting state. Never used by the app itself."""
    city = body.get("city", "Lisbon")
    async with state["db"].acquire() as con:
        await con.execute(RESET_USER_CITY, uid, city)
    await state["redis"].delete(f"user:{uid}")
    while not state["queue"].empty():
        state["queue"].get_nowait()
    return {"reset_to": city, "key_deleted": True}


@app.post("/api/policy")
async def set_policy(body: dict = Body(...)):
    """Switch strategy without a restart. The walkthrough uses this; the
    documented knob is still the WRITE_POLICY environment variable."""
    p = str(body.get("policy", "")).strip().lower()
    if p not in config.POLICIES:
        return JSONResponse(
            {"detail": f"policy must be one of {config.POLICIES}"}, status_code=400
        )
    config.set_write_policy(p)
    print(f"[policy] write_policy={p}", flush=True)
    return {"write_policy": p}
