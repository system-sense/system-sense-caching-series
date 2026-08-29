"""Turns the raw capture logs into capture/metrics.json.

Percentiles and counts rather than single readings, because one race proves
nothing. Every figure the episode puts on screen is read from this file.
"""
import json
import pathlib
import re
import statistics

OUT = pathlib.Path("capture")


def text(name: str) -> str:
    p = OUT / name
    return p.read_text() if p.exists() else ""


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


# ── Episode 1's read path, re-measured here so the callback is this episode's
#    own number rather than a quote from last time. ───────────────────────────
baseline = text("02-baseline-reads.log")
hits = [float(m) for m in re.findall(r"HIT\s+([\d.]+) ms", baseline)]
uncached = re.search(r"uncached\s+([\d.]+) ms", baseline)
read_hit = summarise("cache_hit", hits)
read_uncached_ms = float(uncached.group(1)) if uncached else None

# ── The four policies, raced identically. ────────────────────────────────────
races = {}
for p in ("update", "delete", "write_through", "write_behind"):
    f = OUT / f"race-{p.replace('_', '-')}.json"
    if f.exists():
        races[p] = json.loads(f.read_text())

# ── How long a stale key stays stale. ────────────────────────────────────────
stale_log = text("04-stale-window.log")
samples = re.findall(
    r"t\+\s*(\d+)s\s+db=(\S+)\s+cache=(\S+)\s+agree=(\S+)\s+ttl=(\d+)s", stale_log
)
stale_window = {
    "observed_s": int(samples[-1][0]) if samples else None,
    "still_stale_throughout": bool(samples) and all(s[3] == "False" for s in samples),
    "db_city": samples[-1][1] if samples else None,
    "cache_city": samples[-1][2] if samples else None,
    "ttl_remaining_s": int(samples[-1][4]) if samples else None,
    # Nothing repairs it. The only clock running is the TTL that was set when
    # the wrong value was written.
    "repaired_by": "cache TTL expiry, and nothing else",
}

# ── Write-behind, killed before it flushed. ──────────────────────────────────
loss_log = text("08-write-behind-loss.log")
queued = re.search(r"db=(\S+)\s+cache=(\S+)\s+queue_depth=(\d+)", loss_log)
after = re.findall(r"db=(\S+)\s+cache=(\S+)\s+agree=(\S+)", loss_log)
write_behind_loss = {
    "flush_interval_ms": 10000,
    "queued_db_city": queued.group(1) if queued else None,
    "queued_cache_city": queued.group(2) if queued else None,
    "queue_depth": int(queued.group(3)) if queued else None,
    "db_city_after_restart": after[-1][0] if after else None,
    "cache_city_after_restart": after[-1][1] if after else None,
    "write_lost": bool(after) and after[-1][0] != after[-1][1],
}

# ── What correctness costs on the next read. ─────────────────────────────────
cost = {}
current = None
for line in text("09-cost-of-correctness.log").splitlines():
    m = re.match(r"policy=(\S+)", line.strip())
    if m:
        current = m.group(1)
        cost[current] = {"first": [], "second": [], "first_status": set()}
        continue
    if not current:
        continue
    f = re.search(r"first read after write\s+(\S+)\s+([\d.]+) ms", line)
    if f:
        cost[current]["first_status"].add(f.group(1))
        cost[current]["first"].append(float(f.group(2)))
    s = re.search(r"second read\s+(\S+)\s+([\d.]+) ms", line)
    if s:
        cost[current]["second"].append(float(s.group(2)))

read_after_write = {
    p: {
        "first_read_status": "/".join(sorted(v["first_status"])),
        "first_read_median_ms": round(statistics.median(v["first"]), 2) if v["first"] else None,
        "second_read_median_ms": round(statistics.median(v["second"]), 2) if v["second"] else None,
        "samples": len(v["first"]),
    }
    for p, v in cost.items()
}

miss_penalty = None
if read_after_write.get("delete", {}).get("first_read_median_ms") and read_hit.get("median_ms"):
    miss_penalty = round(
        read_after_write["delete"]["first_read_median_ms"] - read_hit["median_ms"], 2
    )

metrics = {
    "race": {
        "iterations": races.get("update", {}).get("iterations"),
        "stall_ms": races.get("update", {}).get("stall_ms"),
        "lag_ms": races.get("update", {}).get("lag_ms"),
        "policies": {
            p: {
                "stale": r["stale"],
                "consistent": r["consistent"],
                "stale_pct": r["stale_pct"],
                "db_city": r["db_city"],
                "cache_city": r["cache_city"],
                "next_read_status": r["read_after_race_status"],
                "next_read_median_ms": r["read_after_race_median_ms"],
            }
            for p, r in races.items()
        },
    },
    "stale_window": stale_window,
    "write_behind_loss": write_behind_loss,
    "read_after_write": read_after_write,
    "invalidation_miss_penalty_ms": miss_penalty,
    "cache_hit": read_hit,
    "uncached_read_ms": read_uncached_ms,
    "cache_ttl_seconds": 300,
}

(OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(json.dumps(metrics, indent=2))
