"""The boss fight: one lock, one safe release, and one piece of arithmetic.

Episode 3 ended with a number it could not fix. One hot key expired under load
and 78 identical aggregate queries went to Postgres at once, because nothing in
Cache-Aside says "somebody else is already fetching this".

Three things in this file say it.

    acquire()   SET key token NX PX -- one request wins, the rest are told no.
    release()   the part most tutorials get wrong: a worker whose lock has
                already expired must not delete the lock that replaced it.
    should_refresh()
                XFetch. Recompute *before* the TTL reaches zero, so the
                stampede has no moment to happen in.

Nothing here changes Episode 1's read path, Episode 2's write path or Episode
3's defenses. It wraps the miss.
"""
import math
import random
import time
import uuid

from . import config, metrics

LOCK_PREFIX = "lock:"
META_PREFIX = "meta:"

# The safe release, in four lines of Lua.
#
# Redis runs a script atomically, so the compare and the delete cannot be
# separated by anything -- not by another client, and not by this worker's own
# lock expiring between the two. That gap is the whole bug: see release().
RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def lock_key(key: str) -> str:
    return LOCK_PREFIX + key


def meta_key(key: str) -> str:
    return META_PREFIX + key


def new_token() -> str:
    """Who holds the lock. A lock without a token can only be released
    unsafely, because there is nothing to compare against."""
    return uuid.uuid4().hex


async def acquire(redis, key: str, token: str) -> bool:
    """SET key token NX PX ttl -- the election, in one round trip.

    NX means "only if it does not exist": exactly one of two hundred concurrent
    callers gets True. PX is not a detail -- it is what stops a worker that
    dies mid-rebuild from locking the key forever.
    """
    got = await redis.set(
        lock_key(key), token, nx=True, px=config.lock_ttl_ms()
    )
    metrics.bump("lock_acquired" if got else "lock_denied")
    return bool(got)


async def release(redis, key: str, token: str) -> str:
    """Give the lock back -- and only if it is still yours.

    The bug this prevents needs three things to line up, which is why it
    survives code review and shows up in production at 3am:

        1. worker A takes the lock with a 200 ms TTL
        2. A's rebuild takes 400 ms, so the lock expires while A is still
           working -- Redis hands it to worker B
        3. A finishes and deletes "the lock". It deletes B's.

    Now B is rebuilding with no lock, C acquires immediately, and the stampede
    is back with an extra step. The `unsafe` branch below is that code, kept so
    the failure can be measured rather than described.
    """
    lk = lock_key(key)

    if config.lock_release() == "lua":
        removed = await redis.eval(RELEASE_LUA, 1, lk, token)
        metrics.bump("released_own" if removed else "release_refused")
        return "released" if removed else "refused"

    # ── the unsafe version: DEL, whoever it belongs to ───────────────────────
    # The GET is not part of the bug and is not a fix for it -- it is only here
    # so the wrongful deletes can be counted instead of asserted. The DEL below
    # happens either way, which is exactly what a plain `redis.delete(lk)`
    # does in the tutorials this is drawn from.
    current = await redis.get(lk)
    await redis.delete(lk)

    if current is None:
        metrics.bump("release_gone")       # already expired; deleted nothing
        return "gone"
    if current != token:
        metrics.bump("release_wrongful")   # deleted somebody else's lock
        return "wrongful"
    metrics.bump("released_own")
    return "released"


# ── XFetch: the stampede that never gets a moment to happen ──────────────────
#
#     Δt − β · δ · ln(rand()) > TTL_remaining   →   recompute now
#
# δ is the measured cost of the last recompute and β tunes eagerness. ln(rand())
# is negative, so the left-hand side is always positive: every reader rolls its
# own dice, and a reader is more likely to volunteer the closer the key is to
# expiry and the more expensive the key is to rebuild. Expensive keys get
# refreshed earlier than cheap ones without anyone configuring that.
#
# From Vattani, Chierichetti & Lowenstein, "Optimal Probabilistic Cache
# Stampede Prevention" (VLDB 2015).


def should_refresh(delta_s: float, expiry_unix: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    remaining = expiry_unix - now
    if remaining <= 0:
        return True
    # -ln(rand()) is an exponential draw; delta scales it by what a rebuild
    # actually costs, measured on the last one.
    early = config.xfetch_beta() * delta_s * -math.log(random.random() or 1e-12)
    return early >= remaining


async def write_meta(redis, key: str, delta_s: float, ttl_s: int) -> None:
    """What XFetch needs to make that decision, next to the value it is about.

    Kept in its own key on purpose: `user:42` stays byte-for-byte what
    Episodes 1 to 3 wrote, so every endpoint and every viewer's `redis-cli GET`
    still sees the same object.
    """
    await redis.setex(
        meta_key(key),
        ttl_s,
        f"{delta_s:.6f}|{time.time() + ttl_s:.3f}",
    )


def read_meta(raw: str | None) -> tuple[float, float] | None:
    if not raw or "|" not in raw:
        return None
    delta_s, expiry = raw.split("|", 1)
    try:
        return float(delta_s), float(expiry)
    except ValueError:
        return None
