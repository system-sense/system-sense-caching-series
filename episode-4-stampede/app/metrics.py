"""Counters, and a curve.

Two claims in this episode need evidence that a single number cannot carry:
"the database took the whole wave" and "the wave flattened". Both are shapes
over time, so the app samples itself four times a second and keeps the series.

Nothing here is estimated. `db_loads` is incremented in exactly one place --
the function that runs the profile query -- so it counts queries that Postgres
really executed.

Episode 4 adds two things a lock needs to be judged on. `waiting` is how many
readers are asleep waiting for the winner: they are the requests a naive cache
would have sent to Postgres, so the peak of it is the stampede that did not
happen. `pool_in_use` is how many of the twenty Postgres connections are held
right now, which is what "the database connections maxed out" means when it is
a number rather than an adjective.
"""
import time
from collections import deque

SAMPLE_MS = 250
WINDOW_SAMPLES = 4 * 300  # five minutes at 4 Hz

KEYS = (
    "requests",        # every read that arrived
    "hits",            # served from Redis
    "misses",          # not in Redis, went to Postgres
    "db_loads",        # profile queries actually executed
    "not_found",       # ids with no row behind them
    "neg_hits",        # answered from a tombstone
    "bloom_rejects",   # answered by the filter, no Redis, no Postgres
    "false_positives", # filter said "probably", Postgres said no
    # -- Episode 4 --------------------------------------------------------
    "wait_hits",       # lost the lock election, then read what the winner wrote
    "wait_timeouts",   # waited out LOCK_WAIT_MS and went to Postgres anyway
    "early_refreshes", # XFetch: rebuilt before the key expired
    "lock_acquired",   # SET NX returned OK -- this request rebuilds
    "lock_denied",     # SET NX returned nil -- somebody else is rebuilding
    "released_own",    # gave back a lock that was still ours
    "release_refused", # the Lua script refused: the lock had moved on
    "release_wrongful",# the unsafe release deleted somebody else's lock
    "release_gone",    # the unsafe release found nothing to delete
)

counters = {k: 0 for k in KEYS}
inflight = {"db_loads": 0, "max_db_loads": 0, "waiting": 0, "max_waiting": 0}
# Sampled, not counted: what the connection pool looks like right now. main.py
# refreshes these once per tick, straight from asyncpg.
gauges = {"pool_size": 0, "pool_in_use": 0}
per_uid_loads: dict[int, int] = {}
series: deque = deque(maxlen=WINDOW_SAMPLES)
_marks: list = []
_last = dict(counters)
_started = time.time()


def bump(name: str, n: int = 1) -> None:
    counters[name] += n


def enter_load(uid: int) -> None:
    """A profile query is starting. Track concurrency, because Cache Breakdown
    is not "the database got slow" -- it is N identical queries in flight at
    once for the same key."""
    inflight["db_loads"] += 1
    inflight["max_db_loads"] = max(inflight["max_db_loads"], inflight["db_loads"])
    per_uid_loads[uid] = per_uid_loads.get(uid, 0) + 1


def exit_load() -> None:
    inflight["db_loads"] -= 1


def enter_wait() -> None:
    """A reader that lost the lock election is now asleep. Under a stampede
    this is where the other 199 requests are -- not at the database."""
    inflight["waiting"] += 1
    inflight["max_waiting"] = max(inflight["max_waiting"], inflight["waiting"])


def exit_wait() -> None:
    inflight["waiting"] -= 1


def set_gauge(name: str, value) -> None:
    gauges[name] = value


def mark(label: str) -> None:
    """Stamp the timeline: 'keys expired here', 'defense switched on here'."""
    _marks.append({"t": round(time.time() - _started, 3), "label": label})


def reset() -> None:
    global _last, _started
    for k in KEYS:
        counters[k] = 0
    inflight["max_db_loads"] = 0
    inflight["max_waiting"] = 0
    per_uid_loads.clear()
    series.clear()
    _marks.clear()
    _last = dict(counters)
    _started = time.time()


def sample() -> None:
    """One bucket. Rates are per second, so the chart reads in queries/sec
    regardless of what SAMPLE_MS is set to."""
    global _last
    now = counters.copy()
    scale = 1000 / SAMPLE_MS
    series.append(
        {
            "t": round(time.time() - _started, 3),
            **{k: round((now[k] - _last[k]) * scale, 1) for k in KEYS},
            "inflight_db_loads": inflight["db_loads"],
            "waiting_on_lock": inflight["waiting"],
            "pool_in_use": gauges["pool_in_use"],
        }
    )
    _last = now


def snapshot() -> dict:
    hot = sorted(per_uid_loads.items(), key=lambda kv: -kv[1])[:5]
    return {
        "elapsed_s": round(time.time() - _started, 2),
        "counters": counters.copy(),
        "max_concurrent_db_loads": inflight["max_db_loads"],
        "max_waiting_on_lock": inflight["max_waiting"],
        "pool": dict(gauges),
        "hottest_keys": [{"uid": u, "db_loads": c} for u, c in hot],
        "distinct_keys_loaded": len(per_uid_loads),
        "marks": list(_marks),
    }


def timeseries() -> dict:
    return {
        "sample_ms": SAMPLE_MS,
        "marks": list(_marks),
        "samples": list(series),
        "peak_db_loads_per_s": max((s["db_loads"] for s in series), default=0.0),
        "peak_waiting_on_lock": max((s["waiting_on_lock"] for s in series), default=0),
        "peak_pool_in_use": max((s["pool_in_use"] for s in series), default=0),
    }
