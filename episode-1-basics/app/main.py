"""System Sense -- Episode 1: Cache-Aside.

Run it:      docker compose up --build
Try it:      curl -i localhost:8000/api/users/42
The knob:    CACHE_ENABLED=true docker compose up --build
"""
import json
import time
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from . import config
from .cache import get_profile
from .queries import PROFILE_STATS, RECENT_ORDERS, SELECT_USER

state: dict = {}


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
    print(
        f"[startup] cache_enabled={config.CACHE_ENABLED} "
        f"ttl={config.CACHE_TTL_SECONDS}s",
        flush=True,
    )
    yield
    await state["db"].close()
    await state["redis"].aclose()


app = FastAPI(title="System Sense -- Episode 1", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "cache_enabled": config.CACHE_ENABLED}


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


@app.get("/api/users/{uid}/explain")
async def explain_user(uid: int):
    """EXPLAIN ANALYZE for the expensive query, as plain text."""
    async with state["db"].acquire() as con:
        rows = await con.fetch(f"EXPLAIN ANALYZE {PROFILE_STATS}", uid)
    return Response("\n".join(r[0] for r in rows), media_type="text/plain")
