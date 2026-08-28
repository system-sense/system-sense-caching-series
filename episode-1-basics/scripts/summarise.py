"""Turns the raw capture logs into capture/metrics.json.

Percentiles rather than a single reading, because one request proves nothing --
and the median is what the episode quotes on screen.
"""
import json
import pathlib
import re
import statistics

OUT = pathlib.Path("capture")


def times(path: pathlib.Path) -> list[float]:
    if not path.exists():
        return []
    return [float(m) for m in re.findall(r"([\d.]+) ms", path.read_text())]


def summarise(name: str, values: list[float]) -> dict:
    if not values:
        return {"name": name, "samples": 0}
    s = sorted(values)
    pick = lambda q: s[min(len(s) - 1, int(q * len(s)))]
    return {
        "name": name,
        "samples": len(s),
        "min_ms": round(s[0], 2),
        "median_ms": round(statistics.median(s), 2),
        "p95_ms": round(pick(0.95), 2),
        "max_ms": round(s[-1], 2),
    }


off = summarise("cache_off", times(OUT / "03-cache-off.log"))
# The first cache-on request is still a MISS by definition; it populates the key.
on_all = times(OUT / "05-cache-on.log")
on = summarise("cache_on", on_all[1:] if len(on_all) > 1 else on_all)

explain = (OUT / "02-explain-analyze.log").read_text() if (OUT / "02-explain-analyze.log").exists() else ""
plan_ms = re.search(r"Execution Time: ([\d.]+) ms", explain)
redis_log = (OUT / "06-redis-memory.log").read_text() if (OUT / "06-redis-memory.log").exists() else ""
mem = re.search(r"used_memory_human:(\S+)", redis_log)
key_bytes = re.search(r"user:42 bytes: (\d+)", redis_log)

speedup = round(off["median_ms"] / on["median_ms"], 1) if on.get("median_ms") else None

metrics = {
    "cache_off": off,
    "cache_on": on,
    "speedup_x": speedup,
    "postgres_execution_ms": float(plan_ms.group(1)) if plan_ms else None,
    "redis_used_memory_human": mem.group(1) if mem else None,
    "cached_key_bytes": int(key_bytes.group(1)) if key_bytes else None,
}

(OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(json.dumps(metrics, indent=2))
