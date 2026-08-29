"""Reproduce the dual-write race, on demand, as many times as you like.

    python3 scripts/race.py --iterations 20 --stall 400 --lag 150

Two writers change the same user. The first one to reach Postgres is delayed on
its way to Redis; the second one is not. Whoever touches the cache last wins the
cache -- and it is not the one who won the database.

    T1  UPDATE db = Toronto
    T2  UPDATE db = Berlin
    T2  SET cache = Berlin
    T1  SET cache = Toronto     <-- the loser writes last

Every iteration resets the user to a known city first, so this is a repeatable
experiment rather than a lucky screenshot. The result of each run is decided by
one question only: does the cache agree with the database afterwards?
"""
import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
UID = 42
BASELINE = "Lisbon"
WRITER_A = "Toronto"
WRITER_B = "Berlin"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read() or b"{}")
        body["_headers"] = dict(r.headers)
        return body


def read(uid: int = UID) -> dict:
    req = urllib.request.Request(f"{BASE}/api/users/{uid}")
    with urllib.request.urlopen(req, timeout=30) as r:
        profile = json.loads(r.read())
        return {
            "city": profile["city"],
            "status": r.headers.get("X-Cache", "?"),
            "ms": float(r.headers.get("X-Elapsed-Ms", 0)),
        }


def one_race(stall: int, lag_ms: int, flush_wait: float) -> dict:
    call("POST", f"/api/users/{UID}/reset", {"city": BASELINE})
    read()  # warm the cache, so there is something stale to leave behind

    result: dict = {}

    def writer_a():
        result["a"] = call("PUT", f"/api/users/{UID}", {"city": WRITER_A, "stall_ms": stall})

    ta = threading.Thread(target=writer_a)
    ta.start()
    time.sleep(lag_ms / 1000)
    result["b"] = call("PUT", f"/api/users/{UID}", {"city": WRITER_B, "stall_ms": 0})
    ta.join()

    if flush_wait:
        time.sleep(flush_wait)

    after_race = call("GET", f"/api/users/{UID}/truth")
    reader = read()                                   # what the next visitor sees
    after_read = call("GET", f"/api/users/{UID}/truth")

    return {
        "db_city": after_read["db_city"],
        "cache_city": after_read["cache_city"],
        "cached_after_race": after_race["cached"],
        "read_city": reader["city"],
        "read_status": reader["status"],
        "read_ms": reader["ms"],
        "ttl": after_race["ttl"],
        "agree": after_read["agree"] and after_read["cache_city"] == after_read["db_city"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--stall", type=int, default=400)
    ap.add_argument("--lag", type=int, default=150)
    ap.add_argument("--flush-wait", type=float, default=0.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    policy = call("GET", "/health").get("write_policy", "?")
    label = args.label or policy
    print(f"race: policy={policy} iterations={args.iterations} "
          f"stall={args.stall}ms lag={args.lag}ms")
    print(f"      writer A -> {WRITER_A} (stalled), writer B -> {WRITER_B}, "
          f"baseline {BASELINE}")
    print()

    runs = []
    for i in range(1, args.iterations + 1):
        r = one_race(args.stall, args.lag, args.flush_wait)
        runs.append(r)
        verdict = "OK   " if r["agree"] else "STALE"
        print(
            f"  run {i:<3} db={r['db_city']:<9} cache={str(r['cache_city']):<9} "
            f"next-read={r['read_city']:<9} {r['read_status']:<5} "
            f"{r['read_ms']:6.2f} ms   {verdict}"
        )

    stale = [r for r in runs if not r["agree"]]
    reads = [r["read_ms"] for r in runs]
    summary = {
        "policy": policy,
        "label": label,
        "iterations": len(runs),
        "stall_ms": args.stall,
        "lag_ms": args.lag,
        "stale": len(stale),
        "consistent": len(runs) - len(stale),
        "stale_pct": round(100 * len(stale) / len(runs), 1) if runs else 0.0,
        "read_after_race_median_ms": round(statistics.median(reads), 2) if reads else None,
        "read_after_race_status": sorted({r["read_status"] for r in runs}),
        "db_city": runs[-1]["db_city"] if runs else None,
        "cache_city": runs[-1]["cache_city"] if runs else None,
        "ttl_on_stale_key": runs[-1]["ttl"] if runs else None,
    }

    print()
    print(f"  {summary['stale']}/{summary['iterations']} runs left the cache "
          f"disagreeing with the database  ({summary['stale_pct']}%)")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(summary, fh, indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        print(f"cannot reach {BASE}: {exc}", file=sys.stderr)
        sys.exit(1)
