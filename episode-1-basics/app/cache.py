"""Cache-Aside -- the entire pattern, in one function.

This is what Episode 1 is about. Read it top to bottom:

    1. Ask the cache first.
    2. HIT  -> return it, never touch the database.
    3. MISS -> run the real query.
    4. Populate the cache so the next reader does not pay that cost.

That is all Cache-Aside is. Everything else in this repo is plumbing.
"""
import json

from . import config


async def get_profile(uid: int, redis, db, load):
    key = f"user:{uid}"

    if config.CACHE_ENABLED:
        hit = await redis.get(key)
        if hit:
            return json.loads(hit), "HIT"

    profile = await load(uid, db)          # MISS: the basement run
    if profile is None:
        return None, "MISS"

    if config.CACHE_ENABLED:
        await redis.setex(key, config.CACHE_TTL_SECONDS, json.dumps(profile))

    return profile, "MISS"
