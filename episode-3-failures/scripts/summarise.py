"""Turns the raw capture output into capture/metrics.json.

Every figure the episode puts on screen is read from this file, and every
figure in this file came out of a run: k6 measured the traffic, the app counted
its own database calls, Postgres counted them independently, and `docker stats`
watched the CPU. Nothing here is derived from a rule of thumb.
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
    """Postgres CPU, as sampled by `docker stats` during that attack."""
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
    """Postgres' own count of the two queries this app runs.

    Worth keeping separate: a phantom id stops at the user lookup and never
    reaches the aggregate, which is exactly why Penetration is a volume
    problem rather than an expensive-query problem. Everything else in
    pg_stat_statements here is asyncpg resetting pooled connections.
    """
    out = {"user_lookups": None, "profile_aggregates": None}
    for r in pg.get("top", []):
        q = r.get("query", "")
        if q.startswith("SELECT id, name, email"):
            out["user_lookups"] = r["calls"]
        elif "WITH spend AS" in q:
            out["profile_aggregates"] = r["calls"]
    return out


def phase(name: str) -> dict:
    """What the app counted and what Postgres counted, for one attack."""
    m = load(f"m-{name}.json", {}) or {}
    s = load(f"s-{name}.json", {}) or {}
    pg = load(f"pg-{name}.json", {}) or {}
    counters = m.get("counters", {})
    return {
        "counters": counters,
        "max_concurrent_db_loads": m.get("max_concurrent_db_loads"),
        "distinct_keys_loaded": m.get("distinct_keys_loaded"),
        "hottest_keys": m.get("hottest_keys", []),
        "peak_db_loads_per_s": s.get("peak_db_loads_per_s"),
        # Postgres' own count, independent of anything the app said about itself.
        "pg_calls": pg_calls(pg),
    }


def k6(name: str) -> dict:
    return load(f"{name}.json", {}) or {}


def per_request(counters: dict, key: str = "db_loads") -> float | None:
    n = counters.get("requests")
    return round(counters.get(key, 0) / n, 4) if n else None


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

# ── The ceiling every "the database fell over" claim is measured against ─────
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

# ── Failure 1: Penetration ───────────────────────────────────────────────────
health = load("health.json", {}) or {}
bloom = health.get("bloom") or {}

pen = {}
for key, k6name, mname in (
    ("undefended", "04-penetration-none", "penetration-none"),
    ("null_cache_repeated_ids", "06a-penetration-null-repeat", "penetration-null-repeat"),
    ("null_cache_random_ids", "06b-penetration-null-random", "penetration-null-random"),
    ("bloom_filter", "06c-penetration-bloom", "penetration-bloom"),
):
    run, ph = k6(k6name), phase(mname)
    c = ph["counters"]
    pen[key] = {
        "requests": c.get("requests"),
        "db_queries": c.get("db_loads"),
        "db_queries_per_request": per_request(c),
        "served_by": run.get("served_by", {}),
        "rps": run.get("rps"),
        "p95_ms": (run.get("latency_ms") or {}).get("p95"),
        "pg_calls": ph["pg_calls"],
    }

# The Bloom filter's whole cost is its false-positive rate. Measure it, don't
# quote the formula.
bloom_c = phase("penetration-bloom")["counters"]
if bloom_c.get("requests"):
    pen["bloom_filter"]["false_positives"] = bloom_c.get("false_positives")
    pen["bloom_filter"]["measured_fp_rate"] = round(
        bloom_c.get("false_positives", 0) / bloom_c["requests"], 4
    )

penetration = {
    "phantom_id_range": "1,000,000 - 2,000,000 (100,000 users exist)",
    "runs": pen,
    "bloom": {
        "ids": bloom.get("items"),
        "size_kib": bloom.get("size_kib"),
        "bits": bloom.get("bits"),
        "hashes": bloom.get("hashes"),
        "target_fp_rate": bloom.get("target_fp_rate"),
        "predicted_fp_rate": bloom.get("predicted_fp_rate"),
        "build_ms": health.get("bloom_build_ms"),
    },
    "negative_ttl_seconds": health.get("negative_ttl_seconds"),
}

# ── Failure 2: Avalanche ─────────────────────────────────────────────────────
def avalanche_run(warm_file, ttl_file, k6name, mname, cpu_phase) -> dict:
    warm = load(warm_file, {}) or {}
    ttls = load(ttl_file, {}) or {}
    run, ph = k6(k6name), phase(mname)
    lat = run.get("latency_by_layer", {})
    return {
        "keys_warmed": warm.get("keys"),
        "ttl_base_s": warm.get("ttl_base_s"),
        "jitter_s": warm.get("jitter_s"),
        "ttl_spread_s": ttls.get("spread_s"),
        "warm_aggregate_query_ms": warm.get("aggregate_query_ms"),
        "warm_total_ms": warm.get("total_ms"),
        "requests": run.get("requests"),
        "offered_rps": run.get("rps"),
        "db_queries": ph["counters"].get("db_loads"),
        "peak_db_loads_per_s": ph["peak_db_loads_per_s"],
        "pg_calls": ph["pg_calls"],
        "hit_latency_ms": lat.get("hit"),
        "miss_latency_ms": lat.get("miss"),
        "p95_ms": (run.get("latency_ms") or {}).get("p95"),
        "max_ms": (run.get("latency_ms") or {}).get("max"),
        "failed_pct": run.get("failed_pct"),
        "postgres_cpu": cpu(cpu_phase),
    }


fixed = avalanche_run(
    "07a-warm-fixed.json", "07a-ttls-fixed.json",
    "07a-avalanche-fixed", "avalanche-fixed", "avalanche-fixed",
)
jitter = avalanche_run(
    "07b-warm-jitter.json", "07b-ttls-jitter.json",
    "07b-avalanche-jitter", "avalanche-jitter", "avalanche-jitter",
)

avalanche = {"fixed_ttl": fixed, "jittered_ttl": jitter}
if fixed.get("peak_db_loads_per_s") and jitter.get("peak_db_loads_per_s"):
    avalanche["peak_reduction_x"] = round(
        fixed["peak_db_loads_per_s"] / max(jitter["peak_db_loads_per_s"], 0.1), 1
    )
if fixed.get("p95_ms") and jitter.get("p95_ms"):
    avalanche["p95_reduction_x"] = round(fixed["p95_ms"] / max(jitter["p95_ms"], 0.01), 1)

# ── Failure 3: Breakdown -- named, demoed, left broken ───────────────────────
run, ph = k6("08-breakdown"), phase("breakdown")
lat = run.get("latency_by_layer", {})
hot = next((h for h in ph["hottest_keys"] if h["uid"] == 42), None)
breakdown = {
    "hot_key": "user:42",
    "offered_rps": run.get("rps"),
    "requests": run.get("requests"),
    "served_by": run.get("served_by", {}),
    # The number that names the failure: identical queries in flight at once
    # for a single key, because nothing coordinates the misses.
    "max_concurrent_db_loads": ph["max_concurrent_db_loads"],
    "db_queries_for_hot_key": hot["db_loads"] if hot else None,
    "distinct_keys_loaded": ph["distinct_keys_loaded"],
    "peak_db_loads_per_s": ph["peak_db_loads_per_s"],
    "pg_calls": ph["pg_calls"],
    "hit_latency_ms": lat.get("hit"),
    "miss_latency_ms": lat.get("miss"),
    "max_ms": (run.get("latency_ms") or {}).get("max"),
    "postgres_cpu": cpu("breakdown"),
    "fix": "deferred to Episode 4 -- needs a distributed lock, not a TTL",
}

metrics = {
    "baseline": {"cache_hit": cache_hit, "uncached_read_ms": float(uncached.group(1)) if uncached else None},
    "db_ceiling": db_ceiling,
    "cache_ceiling": cache_ceiling,
    "penetration": penetration,
    "avalanche": avalanche,
    "breakdown": breakdown,
    "config": {
        "cache_ttl_seconds": health.get("cache_ttl_seconds"),
        "write_policy": health.get("write_policy"),
        "users": bloom.get("items"),
    },
}

(OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(json.dumps(metrics, indent=2))
