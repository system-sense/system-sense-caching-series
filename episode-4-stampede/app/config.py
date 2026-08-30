"""Configuration.

Episode 1 had one knob: CACHE_ENABLED. Episode 2 added WRITE_POLICY and ended
by concluding that `delete` is the one to use -- so that is the default here,
and this episode leaves the write path alone entirely.

Episode 3 attacked the same app with *traffic*, and its two knobs are still
here, still doing what they did:

    PENETRATION_DEFENSE   none | null_cache | bloom
    TTL_JITTER_SECONDS    0 = every key expires at the same instant

Episode 4 adds the one Episode 3 said it could not fix:

    STAMPEDE_DEFENSE      none | lock | xfetch
    LOCK_TTL_MS           how long one rebuild is allowed to hold the key
    LOCK_RELEASE          unsafe | lua
    XFETCH_BETA           how eagerly a reader volunteers to refresh early
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

# ── Episode 4: the stampede ──────────────────────────────────────────────────
#
#   none     Episode 3's behaviour, unchanged. Every reader that misses runs
#            the aggregate. Two hundred concurrent readers, two hundred
#            queries. (the default, because it is the bug)
#   lock     SET key token NX PX. One reader rebuilds, the rest wait a few
#            milliseconds and read what it wrote.
#   xfetch   probabilistic early refresh. A reader may volunteer to rebuild
#            *before* the TTL runs out, so the key never expires under load
#            and there is no instant for a stampede to happen in.
#
STAMPEDE_DEFENSE = os.getenv("STAMPEDE_DEFENSE", "none").strip().lower()
STAMPEDES = ("none", "lock", "xfetch")

# The lock's own TTL. Too long and a crashed worker blocks the key; too short
# and the lock expires under a slow rebuild -- which is the failure the Lua
# release exists to survive, and the documented experiment in the README.
LOCK_TTL_MS = int(os.getenv("LOCK_TTL_MS", "5000"))

# How long a reader that lost the election waits for the winner, and how often
# it looks. 20 ms is well under a rebuild and well over a Redis round trip.
LOCK_WAIT_MS = int(os.getenv("LOCK_WAIT_MS", "5000"))
LOCK_POLL_MS = int(os.getenv("LOCK_POLL_MS", "20"))

#   lua      compare-and-delete in one atomic script. Correct.
#   unsafe   DEL, whoever it belongs to. What most tutorials show.
LOCK_RELEASE = os.getenv("LOCK_RELEASE", "lua").strip().lower()
RELEASES = ("lua", "unsafe")

# beta > 1 refreshes earlier, beta < 1 later. 1.0 is the paper's default.
XFETCH_BETA = float(os.getenv("XFETCH_BETA", "1.0"))

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
    "ttl": CACHE_TTL_SECONDS,
    "stampede": STAMPEDE_DEFENSE,
    "lock_ttl_ms": LOCK_TTL_MS,
    "lock_release": LOCK_RELEASE,
    "beta": XFETCH_BETA,
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


def cache_ttl_seconds() -> int:
    return _current["ttl"]


def set_cache_ttl_seconds(n: int) -> None:
    """A short TTL is how a stampede is made reproducible on a laptop: the key
    expires every few seconds instead of every five minutes, and the same
    moment happens over and over while the load test is running."""
    _current["ttl"] = n


def stampede() -> str:
    return _current["stampede"]


def set_stampede(s: str) -> None:
    _current["stampede"] = s


def lock_ttl_ms() -> int:
    return _current["lock_ttl_ms"]


def set_lock_ttl_ms(n: int) -> None:
    _current["lock_ttl_ms"] = n


def lock_release() -> str:
    return _current["lock_release"]


def set_lock_release(mode: str) -> None:
    _current["lock_release"] = mode


def xfetch_beta() -> float:
    return _current["beta"]


def set_xfetch_beta(b: float) -> None:
    _current["beta"] = b
