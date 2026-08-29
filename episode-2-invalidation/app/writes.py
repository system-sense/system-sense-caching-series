"""The write path -- four policies, one function each.

Episode 1's read path is untouched. Everything that goes wrong in this episode
goes wrong here, in the handful of lines between "the database has changed" and
"the cache knows about it".

`stall_ms` is the honest part. Every one of these policies has a window between
its two writes: in production that window is scheduler jitter, a GC pause, a
retried packet -- microseconds to tens of milliseconds, hit by two concurrent
writers rarely enough to be dismissed as a fluke and often enough to page you.
`stall_ms` widens that window on demand so the failure is reproducible on a
laptop instead of once a fortnight in production. It widens the window; it does
not create it.
"""
import asyncio
import json

from . import config


async def _sleep(stall_ms: int) -> None:
    if stall_ms > 0:
        await asyncio.sleep(stall_ms / 1000)


# ── update: write the database, then write the cache ─────────────────────────
#
#   T1  UPDATE db = v1
#   T2  UPDATE db = v2
#   T2  SET cache = v2
#   T1  SET cache = v1      ◀── the slow writer wins the cache and loses the DB
#
# The cache now holds v1, the database holds v2, and nothing will ever notice.
# No error, no alarm, no self-correction until the TTL expires.
#
async def update(uid, key, apply_db, rebuild, redis, stall_ms):
    await apply_db()
    profile = await rebuild()   # the value THIS writer believes is now true
    await _sleep(stall_ms)      # ...and then something delays it
    await redis.setex(key, config.CACHE_TTL_SECONDS, json.dumps(profile))
    return "SET"


# ── delete: write the database, then delete the key ──────────────────────────
#
# The same race happens. It just no longer matters: two writers racing to
# delete the same key both leave it deleted, and a deleted key is not a wrong
# answer -- it is a MISS, and a MISS re-reads the database.
#
# Deleting does not make the cache correct. It makes it *unable to be wrong*.
#
async def delete(uid, key, apply_db, rebuild, redis, stall_ms):
    await apply_db()
    await _sleep(stall_ms)
    await redis.delete(key)
    return "DEL"


# ── write_through: cache and database written together ───────────────────────
#
# Sold as the consistent one. At application level it is still two writes in
# some order, so it races exactly like `update` -- measured, in this repo.
# What it genuinely buys is the read after the write: the key is warm, so
# nobody pays the miss.
#
async def write_through(uid, key, apply_db, rebuild, redis, stall_ms):
    profile = await rebuild(pending=True)
    await redis.setex(key, config.CACHE_TTL_SECONDS, json.dumps(profile))
    await _sleep(stall_ms)
    await apply_db()
    return "SET+DB"


# ── write_behind: cache now, database later ──────────────────────────────────
#
# The cache is updated immediately and the database write is queued. Writes get
# fast and Postgres gets a quiet life. The bill arrives when the process does
# not survive long enough to flush: those writes are simply gone.
#
async def write_behind(uid, key, apply_db, rebuild, redis, stall_ms, queue):
    profile = await rebuild(pending=True)
    await redis.setex(key, config.CACHE_TTL_SECONDS, json.dumps(profile))
    await _sleep(stall_ms)
    await queue.put(apply_db)
    return "SET+QUEUED"


async def flusher(queue, interval_ms: int, counter: dict) -> None:
    """Drains the write-behind queue every `interval_ms`."""
    while True:
        await asyncio.sleep(interval_ms / 1000)
        while not queue.empty():
            apply_db = await queue.get()
            await apply_db()
            counter["flushed"] += 1
