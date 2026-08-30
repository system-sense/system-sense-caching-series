"""Counters, and a curve.

Two claims in this episode need evidence that a single number cannot carry:
"the database took the whole wave" and "the wave flattened". Both are shapes
over time, so the app samples itself four times a second and keeps the series.

Nothing here is estimated. `db_loads` is incremented in exactly one place --
the function that runs the profile query -- so it counts queries that Postgres
really executed.
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
)

counters = {k: 0 for k in KEYS}
inflight = {"db_loads": 0, "max_db_loads": 0}
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


def mark(label: str) -> None:
    """Stamp the timeline: 'keys expired here', 'defense switched on here'."""
    _marks.append({"t": round(time.time() - _started, 3), "label": label})


def reset() -> None:
    global _last, _started
    for k in KEYS:
        counters[k] = 0
    inflight["max_db_loads"] = 0
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
        }
    )
    _last = now


def snapshot() -> dict:
    hot = sorted(per_uid_loads.items(), key=lambda kv: -kv[1])[:5]
    return {
        "elapsed_s": round(time.time() - _started, 2),
        "counters": counters.copy(),
        "max_concurrent_db_loads": inflight["max_db_loads"],
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
    }
