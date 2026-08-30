"""Cache-Aside -- Episode 1's function, with every episode's fix folded in.

Episode 1 read like this, and the middle of it still does:

    1. Ask the cache first.
    2. HIT  -> return it, never touch the database.
    3. MISS -> run the real query.
    4. Populate the cache so the next reader does not pay that cost.

Every line added since exists because of a specific failure:

    BLOOM_REJECT   Penetration. An id that was never issued is refused here,
                   before Redis and before Postgres.          (Episode 3)
    NEG_HIT        Penetration. "There is no row" is worth caching.
    ttl_seconds()  Avalanche. Two keys written in the same instant should not
                   expire in the same instant.
    _locked_miss() Breakdown. Step 3 is the bug: two hundred readers that all
                   miss at once all run it. One lock, and the other 199 wait
                   for the first one instead.                 (Episode 4)
    _refresh_early()
                   Breakdown, prevented rather than survived: rebuild the key
                   slightly before it expires, so the moment never arrives.

Step 3 is still the only place this app reaches Postgres for a profile.
"""
import asyncio
import json
import random
import time

from . import config, metrics, stampede

# What a cached absence looks like. Distinguishable from a real profile, which
# is always a JSON object, and from a missing key, which is None.
TOMBSTONE = "\x00null"

# Background early-refresh tasks. Held only so the event loop cannot collect
# one mid-flight; nothing waits on them, which is the point.
_refreshing: set = set()


def ttl_seconds() -> int:
    """The Avalanche defense, in one line.

    TTL = base + rand(0, jitter). At jitter 0 every key written in the same
    pass expires in the same instant -- which is the bug, not a setting.
    """
    jitter = config.jitter_seconds()
    return config.cache_ttl_seconds() + (random.randint(0, jitter) if jitter > 0 else 0)


async def _read(redis, key: str):
    """One round trip. Under XFetch the value's metadata comes back with it."""
    if config.stampede() == "xfetch":
        value, meta = await redis.mget(key, stampede.meta_key(key))
        return value, meta
    return await redis.get(key), None


async def get_profile(uid, redis, db, load, bloom=None):
    metrics.bump("requests")
    key = f"user:{uid}"
    defense = config.defense()

    # -- Penetration defense, the cheap one ---------------------------------
    # A membership filter in local memory. No network call, no query, no Redis
    # round trip -- and it is never wrong when it says no.
    if defense == "bloom" and bloom is not None and uid not in bloom:
        metrics.bump("bloom_rejects")
        return None, "BLOOM_REJECT"

    if config.CACHE_ENABLED:
        hit, meta = await _read(redis, key)
        if hit == TOMBSTONE:
            metrics.bump("neg_hits")
            return None, "NEG_HIT"
        if hit:
            metrics.bump("hits")
            # -- XFetch ------------------------------------------------------
            # This reader has been served and is about to leave. On its way out
            # it rolls the dice on rebuilding the key early, while the value it
            # just returned is still perfectly valid.
            if config.stampede() == "xfetch":
                _maybe_refresh_early(uid, key, meta, redis, db, load)
            return json.loads(hit), "HIT"

    metrics.bump("misses")

    # -- Breakdown defense: elect one rebuilder ------------------------------
    if config.CACHE_ENABLED and config.stampede() in ("lock", "xfetch"):
        return await _locked_miss(uid, key, redis, db, load, defense)

    # Episode 3's behaviour, unchanged: everybody who missed goes to Postgres.
    return await _fill(uid, key, redis, db, load, defense, "MISS")


async def _fill(uid, key, redis, db, load, defense, status):
    """Step 3 and step 4: run the real query, then populate the cache."""
    started = time.perf_counter()
    profile = await load(uid, db)          # MISS: the basement run
    delta_s = time.perf_counter() - started

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
        ttl = ttl_seconds()
        await redis.setex(key, ttl, json.dumps(profile))
        if config.stampede() == "xfetch":
            # What the next reader needs in order to decide whether to refresh
            # early: what this rebuild cost, and when the value dies.
            await stampede.write_meta(redis, key, delta_s, ttl)

    return profile, status


async def _locked_miss(uid, key, redis, db, load, defense):
    """One reader rebuilds. The rest wait in the dining room.

    The waiters are not queued at the database and are not holding a Postgres
    connection. They are asleep for a few milliseconds, and then they read what
    the winner wrote -- from Redis, at Redis speed.
    """
    token = stampede.new_token()

    if await stampede.acquire(redis, key, token):
        try:
            return await _fill(uid, key, redis, db, load, defense, "LEADER")
        finally:
            await stampede.release(redis, key, token)

    # Lost the election.
    metrics.enter_wait()
    try:
        deadline = time.monotonic() + config.LOCK_WAIT_MS / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(config.LOCK_POLL_MS / 1000)
            value, _ = await _read(redis, key)
            if value == TOMBSTONE:
                metrics.bump("neg_hits")
                return None, "NEG_HIT"
            if value:
                metrics.bump("wait_hits")
                return json.loads(value), "WAIT_HIT"
    finally:
        metrics.exit_wait()

    # The winner is slower than LOCK_WAIT_MS, or died holding the lock. Going
    # to the database anyway is the honest failure mode: still correct, just no
    # longer coordinated. It is counted so that it can never be silent.
    metrics.bump("wait_timeouts")
    return await _fill(uid, key, redis, db, load, defense, "WAIT_TIMEOUT")


def _maybe_refresh_early(uid, key, meta, redis, db, load) -> None:
    """The XFetch roll, on the way out of a hit.

        beta * delta * -ln(rand())  >=  TTL remaining   ->   rebuild now

    Almost every reader loses this roll and leaves. The one that wins does the
    rebuild in the background, holding the same lock, while the key it is
    replacing is still in the cache answering everybody else. Nobody waits.
    """
    parsed = stampede.read_meta(meta)
    if parsed is None:
        return
    delta_s, expiry = parsed
    if not stampede.should_refresh(delta_s, expiry):
        return

    task = asyncio.create_task(_refresh_early(uid, key, redis, db, load))
    _refreshing.add(task)
    task.add_done_callback(_refreshing.discard)


async def _refresh_early(uid, key, redis, db, load) -> None:
    token = stampede.new_token()
    if not await stampede.acquire(redis, key, token):
        return                             # another reader volunteered first
    metrics.bump("early_refreshes")
    try:
        await _fill(uid, key, redis, db, load, config.defense(), "REFRESH")
    finally:
        await stampede.release(redis, key, token)
