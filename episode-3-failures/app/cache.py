"""Cache-Aside -- Episode 1's function, with the two defenses folded in.

Episode 1 read like this, and the middle of it still does:

    1. Ask the cache first.
    2. HIT  -> return it, never touch the database.
    3. MISS -> run the real query.
    4. Populate the cache so the next reader does not pay that cost.

Every line added below exists because of a specific failure:

    BLOOM_REJECT   Penetration. An id that was never issued is refused here,
                   before Redis and before Postgres.
    NEG_HIT        Penetration. "There is no row" is itself worth caching.
    ttl_seconds()  Avalanche. Two keys written in the same instant should not
                   expire in the same instant.

Note what is *not* here: anything for Cache Breakdown. A hot key that expires
under concurrent load walks straight through step 3, N times at once. That is
Episode 4.
"""
import json
import random

from . import config, metrics

# What a cached absence looks like. Distinguishable from a real profile, which
# is always a JSON object, and from a missing key, which is None.
TOMBSTONE = "\x00null"


def ttl_seconds() -> int:
    """The Avalanche defense, in one line.

    TTL = base + rand(0, jitter). At jitter 0 every key written in the same
    pass expires in the same instant -- which is the bug, not a setting.
    """
    jitter = config.jitter_seconds()
    return config.CACHE_TTL_SECONDS + (random.randint(0, jitter) if jitter > 0 else 0)


async def get_profile(uid, redis, db, load, bloom=None):
    metrics.bump("requests")
    key = f"user:{uid}"
    defense = config.defense()

    # ── Penetration defense, the cheap one ───────────────────────────────────
    # A membership filter in local memory. No network call, no query, no Redis
    # round trip -- and it is never wrong when it says no.
    if defense == "bloom" and bloom is not None and uid not in bloom:
        metrics.bump("bloom_rejects")
        return None, "BLOOM_REJECT"

    if config.CACHE_ENABLED:
        hit = await redis.get(key)
        if hit == TOMBSTONE:
            metrics.bump("neg_hits")
            return None, "NEG_HIT"
        if hit:
            metrics.bump("hits")
            return json.loads(hit), "HIT"

    metrics.bump("misses")
    profile = await load(uid, db)          # MISS: the basement run

    if profile is None:
        metrics.bump("not_found")
        if defense == "bloom":
            # The filter said "probably". Postgres said no. This is the 1%
            # the sizing bought, counted rather than assumed.
            metrics.bump("false_positives")
        if defense in ("null_cache", "bloom") and config.CACHE_ENABLED:
            # Remember the absence, briefly. The second request for a phantom
            # id is answered by Redis; only the first one costs a query.
            await redis.setex(key, config.NEGATIVE_TTL_SECONDS, TOMBSTONE)
        return None, "MISS"

    if config.CACHE_ENABLED:
        await redis.setex(key, ttl_seconds(), json.dumps(profile))

    return profile, "MISS"
