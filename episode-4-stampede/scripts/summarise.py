"""Turns the raw capture output into capture/metrics.json.

Every figure the episode puts on screen is read from this file, and every
figure in this file came out of a run: k6 measured the traffic, the app counted
its own database calls and its own waiters, Postgres counted the queries
independently -- and, for the two headline runs, logged every statement so the
count can be read off Postgres' own log instead of the application's word.
"""
import json
import pathlib
import re
import statistics

OUT = pathlib.Path("capture")


def load(name: str, default=None):
    p = OUT / name
    if not p.exists() or not p.stat().st_size:
        return default
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return default


def text(name: str) -> str:
    p = OUT / name
    return p.read_text() if p.exists() else ""


def cpu(phase: str) -> dict:
    """Postgres CPU, as sampled by `docker stats` during that run."""
    pcts = [float(m) for m in re.findall(r"\s([\d.]+)%", text(f"cpu-{phase}.log"))]
    if not pcts:
        return {"samples": 0}
    out = {
        "samples": len(pcts),
        "median_pct": round(statistics.median(pcts), 1),
        "peak_pct": round(max(pcts), 1),
        "note": "docker stats, ~0.7 Hz; 100% = one core",
    }
    if len(pcts) < 20:
        out["note"] = (
            "docker stats, ~0.7 Hz -- too coarse to resolve an event this short; "
            "use max_concurrent_db_loads and the latencies instead"
        )
    return out


def pg_calls(pg: dict) -> dict:
    """Postgres' own count of the two queries this app runs, independent of
    anything the application said about itself."""
    out = {"user_lookups": None, "profile_aggregates": None}
    for r in pg.get("top", []):
        q = r.get("query", "")
        if q.startswith("SELECT id, name, email"):
            out["user_lookups"] = r["calls"]
        elif "WITH spend AS" in q:
            out["profile_aggregates"] = r["calls"]
    return out


def phase(name: str) -> dict:
    """What the app counted, for one run."""
    m = load(f"m-{name}.json", {}) or {}
    cfg = m.get("config", {})
    s = load(f"s-{name}.json", {}) or {}
    pg = load(f"pg-{name}.json", {}) or {}
    return {
        "counters": m.get("counters", {}),
        "max_concurrent_db_loads": m.get("max_concurrent_db_loads"),
        "max_waiting_on_lock": m.get("max_waiting_on_lock"),
        "hottest_keys": m.get("hottest_keys", []),
        "peak_db_loads_per_s": s.get("peak_db_loads_per_s"),
        "peak_pool_in_use": s.get("peak_pool_in_use"),
        "peak_waiting_on_lock": s.get("peak_waiting_on_lock"),
        "pg_calls": pg_calls(pg),
        # What was in force during this run, not at startup.
        "lock_ttl_ms": cfg.get("lock_ttl_ms"),
        "cache_ttl_seconds": cfg.get("cache_ttl_seconds"),
    }


def k6(name: str) -> dict:
    return load(f"{name}.json", {}) or {}


def offered_rps(name: str) -> float | None:
    """The rate k6 was asked for, from k6's own description of the scenario.

    Not the same as the rate it achieved, and the difference is part of the
    story: under collapse the offered rate stops being achievable.
    """
    m = re.search(r"\* traffic: ([\d.]+) iterations/s", text(f"{name}.log"))
    return float(m.group(1)) if m else None


def lat(run: dict, layer: str | None = None) -> dict | None:
    if layer is None:
        return run.get("latency_ms")
    return (run.get("latency_by_layer") or {}).get(layer)


# ── The callback: Episode 1's read path, re-measured here ────────────────────
baseline = text("02-baseline-reads.log")
hits = [float(m) for m in re.findall(r"HIT\s+([\d.]+) ms", baseline)]
uncached = re.search(r"uncached\s+([\d.]+) ms", baseline)
cache_hit = (
    {
        "samples": len(hits),
        "min_ms": round(min(hits), 2),
        "median_ms": round(statistics.median(hits), 2),
        "max_ms": round(max(hits), 2),
    }
    if hits
    else {"samples": 0}
)

# ── The two ceilings every claim below is measured against ───────────────────
ceiling = k6("03-db-ceiling")
db_ceiling = {
    "profile_queries_per_s": ceiling.get("rps"),
    "median_ms": (ceiling.get("latency_ms") or {}).get("med"),
    "p95_ms": (ceiling.get("latency_ms") or {}).get("p95"),
    "requests": ceiling.get("requests"),
    "note": "uncached reads, straight to Postgres, never touching Redis",
}

cache_run = k6("03b-cache-ceiling")
cache_ceiling = {
    "requests_per_s": cache_run.get("rps"),
    "median_ms": (cache_run.get("latency_ms") or {}).get("med"),
    "p95_ms": (cache_run.get("latency_ms") or {}).get("p95"),
    "failed_pct": cache_run.get("failed_pct"),
    "dropped_iterations": cache_run.get("dropped_iterations"),
    "note": "warmed keys, answered by Redis; an offered rate that was met, not a limit found",
}


# ── The stampede: one key, VUS readers, the instant it expires ───────────────
def burst(k6name: str, mname: str, pg_log_name: str, pg_phase: str, cpu_phase: str) -> dict:
    run, ph = k6(k6name), phase(mname)
    c = ph["counters"]
    hot = ph["hottest_keys"][0] if ph["hottest_keys"] else None
    pg_log = load(f"{pg_log_name}-pg-log.json", {}) or {}
    return {
        # Counted from the X-Cache header on each measured response. The app's
        # own `requests` counter is higher: it also holds each reader's warm-up
        # request, taken while the key was still alive.
        "readers": sum((run.get("served_by") or {}).values()) or None,
        "vus": run.get("vus"),
        "served_by": run.get("served_by", {}),
        # The number that names the failure, and the number that fixes it:
        # how many identical aggregates ran for a single key.
        "db_queries": c.get("db_loads"),
        "db_queries_for_hot_key": hot["db_loads"] if hot else None,
        "max_concurrent_db_loads": c and ph["max_concurrent_db_loads"],
        # Of those, how many were actually inside Postgres at once. The pool
        # holds 20 connections, so this is where "the connections maxed out"
        # stops being an adjective.
        "peak_pool_in_use": ph["peak_pool_in_use"],
        "max_waiting_on_lock": ph["max_waiting_on_lock"],
        "wait_hits": c.get("wait_hits"),
        "wait_timeouts": c.get("wait_timeouts"),
        "lock_acquired": c.get("lock_acquired"),
        "lock_denied": c.get("lock_denied"),
        "latency_ms": lat(run, "reader"),
        "miss_latency_ms": lat(run, "miss"),
        "leader_latency_ms": lat(run, "leader"),
        "wait_latency_ms": lat(run, "waithit"),
        "pg_calls": ph["pg_calls"],
        # Postgres' own log, from a separate identical burst run with
        # log_statement=all. Separate because logging every statement is a real
        # cost and would show up in the latencies above -- so this run has its
        # own reader and query counts, and they are the ones the log count is
        # to be read against.
        "pg_log": {
            "log_lines": pg_log.get("log_lines"),
            "aggregate_executions": pg_log.get("aggregates"),
            "statements_for_hot_key": pg_log.get("statements_for_hot_key"),
            "db_queries": phase(pg_phase)["counters"].get("db_loads"),
        },
        "postgres_cpu": cpu(cpu_phase),
    }


stampede = {
    "hot_key": "user:42",
    "undefended": burst(
        "04-stampede-none", "stampede-none", "05-pg-log-none", "pg-none", "stampede-none"
    ),
    "locked": burst(
        "06-stampede-lock", "stampede-lock", "07-pg-log-lock", "pg-lock", "stampede-lock"
    ),
}
u, l = stampede["undefended"], stampede["locked"]
if u.get("db_queries") and l.get("db_queries"):
    stampede["query_reduction_x"] = round(u["db_queries"] / max(l["db_queries"], 1), 1)
if (u.get("latency_ms") or {}).get("max") and (l.get("latency_ms") or {}).get("max"):
    stampede["slowest_request_reduction_x"] = round(
        u["latency_ms"]["max"] / max(l["latency_ms"]["max"], 0.01), 1
    )


# ── The release: what a lock that expired under its holder does next ─────────
def release(k6name: str, mname: str) -> dict:
    run, ph = k6(k6name), phase(mname)
    c = ph["counters"]
    return {
        "lock_ttl_ms": ph["lock_ttl_ms"],
        "cache_ttl_seconds": ph["cache_ttl_seconds"],
        "requests": run.get("requests"),
        "achieved_rps": run.get("rps"),
        # Requests the load generator wanted to send and could not, because
        # every virtual user was still waiting on its last one.
        "dropped_requests": run.get("dropped_iterations"),
        "lock_acquired": c.get("lock_acquired"),
        "lock_denied": c.get("lock_denied"),
        "released_own": c.get("released_own"),
        # The bug, counted: a worker deleting a lock that had already been
        # handed to somebody else.
        "release_wrongful": c.get("release_wrongful"),
        # The Lua script's answer to the same situation: refuse, and let the
        # real holder keep it.
        "release_refused": c.get("release_refused"),
        "release_gone": c.get("release_gone"),
        "db_queries": c.get("db_loads"),
        "max_concurrent_db_loads": ph["max_concurrent_db_loads"],
        "wait_timeouts": c.get("wait_timeouts"),
        "latency_ms": lat(run),
    }


release_runs = {
    "note": "the same short lock TTL both ways: the only difference is how the lock is given back",
    "unsafe": release("08-release-unsafe", "release-unsafe"),
    "lua": release("09-release-lua", "release-lua"),
}


# ── Sustained traffic: the same key expiring over and over ───────────────────
def sustained(mode: str) -> dict:
    run, ph = k6(f"10-sustained-{mode}"), phase(f"sustained-{mode}")
    c = ph["counters"]
    return {
        "requests": run.get("requests"),
        "achieved_rps": run.get("rps"),
        "dropped_requests": run.get("dropped_iterations"),
        "served_by": run.get("served_by", {}),
        "db_queries": c.get("db_loads"),
        "early_refreshes": c.get("early_refreshes"),
        "wait_hits": c.get("wait_hits"),
        "wait_timeouts": c.get("wait_timeouts"),
        "max_concurrent_db_loads": ph["max_concurrent_db_loads"],
        "max_waiting_on_lock": ph["max_waiting_on_lock"],
        "peak_db_loads_per_s": ph["peak_db_loads_per_s"],
        "peak_pool_in_use": ph["peak_pool_in_use"],
        "latency_ms": lat(run),
        "hit_latency_ms": lat(run, "hit"),
        "miss_latency_ms": lat(run, "miss"),
        "leader_latency_ms": lat(run, "leader"),
        "wait_latency_ms": lat(run, "waithit"),
        "failed_pct": run.get("failed_pct"),
        "pg_calls": ph["pg_calls"],
        "postgres_cpu": cpu(f"sustained-{mode}"),
    }


sus = {m: sustained(m) for m in ("none", "lock", "xfetch")}
health = load("health.json", {}) or {}
sustained_runs = {
    "cache_ttl_seconds": None,   # filled below from the run's own config
    "offered_rps": offered_rps("10-sustained-none"),
    "runs": sus,
}
m_none = load("m-sustained-none.json", {}) or {}
sustained_runs["cache_ttl_seconds"] = (m_none.get("config") or {}).get("cache_ttl_seconds")
# The honest comparison across these three runs is the load they put on
# Postgres, not the slowest single request: at 120,000 requests a max is one
# scheduling hiccup, and all three have one. p99 is in each run above.
for mode in ("lock", "xfetch"):
    if sus["none"].get("db_queries") and sus[mode].get("db_queries"):
        sustained_runs[f"db_query_reduction_none_vs_{mode}_x"] = round(
            sus["none"]["db_queries"] / max(sus[mode]["db_queries"], 1), 1
        )

metrics = {
    "baseline": {
        "cache_hit": cache_hit,
        "uncached_read_ms": float(uncached.group(1)) if uncached else None,
    },
    "db_ceiling": db_ceiling,
    "cache_ceiling": cache_ceiling,
    "stampede": stampede,
    "release": release_runs,
    "sustained": sustained_runs,
    "config": {
        "cache_ttl_seconds": health.get("cache_ttl_seconds"),
        "write_policy": health.get("write_policy"),
        "lock_ttl_ms": health.get("lock_ttl_ms"),
        "lock_wait_ms": health.get("lock_wait_ms"),
        "lock_release": health.get("lock_release"),
        "xfetch_beta": health.get("xfetch_beta"),
        "users": (health.get("bloom") or {}).get("items"),
    },
}

(OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(json.dumps(metrics, indent=2))
