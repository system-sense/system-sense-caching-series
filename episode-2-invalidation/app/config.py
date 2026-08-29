"""Configuration.

Episode 1 had one knob: CACHE_ENABLED. It stays, and it stays on -- Episode 2
takes the working Cache-Aside read path as given and attacks the *write* path.

The knob that matters here is WRITE_POLICY.
"""
import os


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ── Episode 1's knob, inherited ──────────────────────────────────────────────
CACHE_ENABLED = _flag("CACHE_ENABLED", "true")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))

# ── The experiment knob ──────────────────────────────────────────────────────
#
#   update         write the database, then write the cache.   ◀── the bug
#   delete         write the database, then delete the key.    ◀── the fix
#   write_through  write the cache, then the database, together.
#   write_behind   write the cache now, queue the database for later.
#
# Defaults to `update` because that is the one everybody writes first, and this
# episode is about watching it fail.
#
WRITE_POLICY = os.getenv("WRITE_POLICY", "update").strip().lower()

# The env var above is the knob. This holds whatever is in force right now, so
# the walkthrough at localhost:8080 can switch strategy and show the same race
# coming out differently without a container restart. Production code would not
# have this; a demo that makes you restart to see the fix is a worse demo.
_current = {"policy": WRITE_POLICY}


def write_policy() -> str:
    return _current["policy"]


def set_write_policy(p: str) -> None:
    _current["policy"] = p

# How long the write-behind queue waits before flushing to Postgres. Everything
# written and not yet flushed is lost if the process dies. That is the trade.
WRITE_BEHIND_FLUSH_MS = int(os.getenv("WRITE_BEHIND_FLUSH_MS", "1000"))

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgres://sysense:sysense@localhost:5432/sysense"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

POLICIES = ("update", "delete", "write_through", "write_behind")
