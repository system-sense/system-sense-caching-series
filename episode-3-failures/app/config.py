"""Configuration.

Episode 1 had one knob: CACHE_ENABLED. Episode 2 added WRITE_POLICY and ended
by concluding that `delete` is the one to use -- so that is the default here,
and this episode leaves the write path alone entirely.

Episode 3 attacks the same app with *traffic*. Its knobs are the two defenses:

    PENETRATION_DEFENSE   none | null_cache | bloom
    TTL_JITTER_SECONDS    0 = every key expires at the same instant
"""
import os


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ── Episode 1's knob, inherited. Still on, still uninteresting. ──────────────
CACHE_ENABLED = _flag("CACHE_ENABLED", "true")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))

# ── Episode 2's conclusion, inherited as the default ─────────────────────────
#
# `delete` is what Episode 2 measured as the only policy that does not go stale.
# Nothing in this episode changes it; the write path is settled.
#
WRITE_POLICY = os.getenv("WRITE_POLICY", "delete").strip().lower()
WRITE_BEHIND_FLUSH_MS = int(os.getenv("WRITE_BEHIND_FLUSH_MS", "1000"))
POLICIES = ("update", "delete", "write_through", "write_behind")

# ── Episode 3, failure 1: Penetration ────────────────────────────────────────
#
#   none        a request for an id that does not exist misses the cache, and
#               there is nothing to cache, so it reaches Postgres. Every time.
#   null_cache  remember the absence: store a tombstone with a short TTL.
#   bloom       an in-process membership filter answers "definitely not here"
#               without touching Redis or Postgres at all.
#
PENETRATION_DEFENSE = os.getenv("PENETRATION_DEFENSE", "none").strip().lower()
DEFENSES = ("none", "null_cache", "bloom")

# Deliberately short. A tombstone is a guess about a row that does not exist
# yet; holding it for five minutes means five minutes of 404 for whoever
# finally signs up with that id.
NEGATIVE_TTL_SECONDS = int(os.getenv("NEGATIVE_TTL_SECONDS", "30"))

# Target false-positive rate for the Bloom filter. 1% is the textbook default
# and is honest about the trade: 1 in 100 phantom ids still gets through.
BLOOM_FP_RATE = float(os.getenv("BLOOM_FP_RATE", "0.01"))

# ── Episode 3, failure 2: Avalanche ──────────────────────────────────────────
#
# Every cache write gets TTL = base + rand(0, TTL_JITTER_SECONDS). At 0 -- the
# default, and what a batch job writes without thinking -- 100,000 keys warmed
# in one pass expire in the same instant.
#
TTL_JITTER_SECONDS = int(os.getenv("TTL_JITTER_SECONDS", "0"))

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgres://sysense:sysense@localhost:5432/sysense"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Everything below holds what is in force *right now*. The env vars above are
# the documented knobs; these let the walkthrough at localhost:8080 turn a
# defense on mid-attack and watch the graph change, without a restart.
_current = {
    "policy": WRITE_POLICY,
    "defense": PENETRATION_DEFENSE,
    "jitter": TTL_JITTER_SECONDS,
}


def write_policy() -> str:
    return _current["policy"]


def set_write_policy(p: str) -> None:
    _current["policy"] = p


def defense() -> str:
    return _current["defense"]


def set_defense(d: str) -> None:
    _current["defense"] = d


def jitter_seconds() -> int:
    return _current["jitter"]


def set_jitter_seconds(n: int) -> None:
    _current["jitter"] = n
