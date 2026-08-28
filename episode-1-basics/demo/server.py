"""The demo UI's little backend.

It does three things and nothing else:

  * forwards a read to the app and reports what came back,
  * clears the cached key so the walkthrough can be run again,
  * reports what Redis currently holds.

It never fabricates a number. Every latency shown in the browser is the
`X-Elapsed-Ms` the application itself measured for that request.
"""
import asyncio
import os
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_URL = os.getenv("APP_URL", "http://app:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
USER_ID = int(os.getenv("DEMO_USER_ID", "42"))

client = httpx.AsyncClient(timeout=30.0)
rds = aioredis.from_url(REDIS_URL, decode_responses=True)

PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")


async def _announce() -> None:
    """Print the link once the stack can actually serve it.

    Compose interleaves every service's output, and the seed takes a few
    seconds, so a banner printed at import time scrolls away above the noise
    and points at a page that is not ready. Waiting for the app to answer means
    the link is both last and true. Terminals make http:// URLs clickable.
    """
    for _ in range(180):
        try:
            if (await client.get(f"{APP_URL}/health", timeout=2.0)).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)

    rule = "\u2500" * 60
    print(
        f"\n{rule}\n"
        f"\n   System Sense \u00b7 Episode 1 \u2014 Cache-Aside\n"
        f"\n   Open  {PUBLIC_URL}\n"
        f"\n{rule}\n",
        flush=True,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_announce())
    yield
    task.cancel()


app = FastAPI(title="Episode 1 demo", lifespan=lifespan)


def _key(uid: int) -> str:
    return f"user:{uid}"


@app.get("/api/read")
async def read(mode: str = "cached", uid: int = USER_ID):
    """One read. `mode=cached` uses Cache-Aside; `mode=uncached` bypasses it."""
    path = f"/api/users/{uid}" + ("/uncached" if mode == "uncached" else "")
    try:
        r = await client.get(f"{APP_URL}{path}")
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"app unreachable: {exc}"}, status_code=502)

    return {
        "mode": mode,
        "status": r.headers.get("X-Cache", "?"),
        "ms": float(r.headers.get("X-Elapsed-Ms", 0)),
        "cache_enabled": r.headers.get("X-Cache-Enabled") == "true",
        "http": r.status_code,
    }


@app.post("/api/flush")
async def flush(uid: int = USER_ID):
    """Delete the cached key, so the next cached read is a MISS again."""
    removed = await rds.delete(_key(uid))
    return {"deleted": bool(removed), "key": _key(uid)}


@app.get("/api/explain")
async def explain(uid: int = USER_ID):
    r = await client.get(f"{APP_URL}/api/users/{uid}/explain")
    return {"plan": r.text}


@app.get("/api/state")
async def state(uid: int = USER_ID):
    try:
        health = (await client.get(f"{APP_URL}/health")).json()
    except httpx.HTTPError:
        return JSONResponse({"error": "app unreachable"}, status_code=502)

    key = _key(uid)
    ttl = await rds.ttl(key)
    info = await rds.info("memory")
    try:
        size = await rds.memory_usage(key)
    except Exception:
        size = None

    return {
        "cache_enabled": health.get("cache_enabled", False),
        "user_id": uid,
        "key": key,
        "key_present": ttl is not None and ttl >= 0,
        "ttl": ttl if ttl and ttl >= 0 else None,
        "key_bytes": size,
        "redis_human": info.get("used_memory_human"),
        "keys": await rds.dbsize(),
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
