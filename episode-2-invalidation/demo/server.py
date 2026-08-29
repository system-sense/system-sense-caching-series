"""The demo UI's little backend.

It does four things and nothing else:

  * runs the two-writer race against the app and reports the outcome,
  * reports what Postgres and Redis each hold, side by side,
  * switches the write policy,
  * resets the user so the race can be run again from a known state.

It never fabricates a number. Every latency shown in the browser is the
`X-Elapsed-Ms` the application itself measured for that request, and every
city shown is read back out of Postgres and Redis after the fact.
"""
import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_URL = os.getenv("APP_URL", "http://app:8000")
USER_ID = int(os.getenv("DEMO_USER_ID", "42"))

BASELINE = "Lisbon"
WRITER_A = "Toronto"
WRITER_B = "Berlin"

client = httpx.AsyncClient(timeout=60.0)

PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")


async def _announce() -> None:
    """Print the link once the stack can actually serve it."""
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
        f"\n   System Sense · Episode 2 — The Invalidation Nightmare\n"
        f"\n   Open  {PUBLIC_URL}\n"
        f"\n{rule}\n",
        flush=True,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_announce())
    yield
    task.cancel()


app = FastAPI(title="Episode 2 demo", lifespan=lifespan)


async def _truth(uid: int) -> dict:
    return (await client.get(f"{APP_URL}/api/users/{uid}/truth")).json()


async def _reset(uid: int) -> None:
    await client.post(f"{APP_URL}/api/users/{uid}/reset", json={"city": BASELINE})


async def _read(uid: int) -> dict:
    r = await client.get(f"{APP_URL}/api/users/{uid}")
    return {
        "city": r.json().get("city"),
        "status": r.headers.get("X-Cache", "?"),
        "ms": float(r.headers.get("X-Elapsed-Ms", 0)),
    }


async def _write(uid: int, city: str, stall_ms: int) -> dict:
    r = await client.put(
        f"{APP_URL}/api/users/{uid}", json={"city": city, "stall_ms": stall_ms}
    )
    return {
        "city": city,
        "action": r.headers.get("X-Cache-Action", "?"),
        "ms": float(r.headers.get("X-Elapsed-Ms", 0)),
        "net_ms": float(r.headers.get("X-Elapsed-Net-Ms", 0)),
    }


@app.post("/api/race")
async def race(body: dict = Body(default={})):
    """Two writers, one user, one deliberately widened window.

    Writer A reaches Postgres first and is then delayed on its way to Redis.
    Writer B reaches Postgres second and is not delayed. Whoever touches the
    cache last wins the cache -- and it is not the one who won the database.
    """
    uid = int(body.get("uid", USER_ID))
    stall = int(body.get("stall_ms", 400))
    lag = int(body.get("lag_ms", 150))

    await _reset(uid)
    await _read(uid)  # warm the cache, so there is something stale to leave behind

    async def writer_a():
        return await _write(uid, WRITER_A, stall)

    async def writer_b():
        await asyncio.sleep(lag / 1000)
        return await _write(uid, WRITER_B, 0)

    a, b = await asyncio.gather(writer_a(), writer_b())

    policy = (await client.get(f"{APP_URL}/health")).json().get("write_policy")
    if policy == "write_behind":
        await asyncio.sleep(1.5)  # let the flusher drain before we judge it

    after_race = await _truth(uid)
    reader = await _read(uid)
    after_read = await _truth(uid)

    return {
        "policy": policy,
        "stall_ms": stall,
        "lag_ms": lag,
        "writer_a": {**a, "label": f"A → {WRITER_A} (stalled {stall} ms)"},
        "writer_b": {**b, "label": f"B → {WRITER_B}"},
        "cached_after_race": after_race["cached"],
        "db_city": after_read["db_city"],
        "cache_city": after_read["cache_city"],
        "reader": reader,
        "ttl": after_race["ttl"],
        "agree": after_read["cache_city"] == after_read["db_city"],
    }


@app.post("/api/policy")
async def policy(body: dict = Body(...)):
    r = await client.post(f"{APP_URL}/api/policy", json=body)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/reset")
async def reset(uid: int = USER_ID):
    await _reset(uid)
    return await _truth(uid)


@app.get("/api/read")
async def read(uid: int = USER_ID):
    try:
        return await _read(uid)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"app unreachable: {exc}"}, status_code=502)


@app.get("/api/state")
async def state(uid: int = USER_ID):
    try:
        health = (await client.get(f"{APP_URL}/health")).json()
        t = await _truth(uid)
    except httpx.HTTPError:
        return JSONResponse({"error": "app unreachable"}, status_code=502)

    return {
        "write_policy": health.get("write_policy"),
        "cache_enabled": health.get("cache_enabled"),
        "queue_depth": t.get("queue_depth", 0),
        "user_id": uid,
        "db_city": t["db_city"],
        "cache_city": t["cache_city"],
        "cached": t["cached"],
        "ttl": t["ttl"],
        "agree": t["cache_city"] is None or t["db_city"] == t["cache_city"],
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
