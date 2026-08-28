"""Configuration. One knob matters: CACHE_ENABLED."""
import os


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ── The experiment knob ──────────────────────────────────────────────────────
# Flip this to false and every read goes to Postgres. Flip it to true and the
# same reads are served from Redis. Nothing else in the app changes.
CACHE_ENABLED = _flag("CACHE_ENABLED", "false")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgres://sysense:sysense@localhost:5432/sysense"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
