"""The SQL behind a user profile.

A profile page is never one row. This endpoint answers what a real profile
actually shows: who the user is, how much they have spent, where that ranks
them against every other customer, and what they bought recently.

PROFILE_STATS is the expensive one. It aggregates the whole orders table to
work out a percentile, which is exactly the kind of read that is costly to
compute, cheap to store, and changes slowly -- in other words, the textbook
candidate for a cache.
"""

# Cheap: primary-key lookup.
SELECT_USER = """
SELECT id, name, email, city, joined_at
FROM   users
WHERE  id = $1
"""

# Expensive: aggregates every order in the table to rank this user.
PROFILE_STATS = """
WITH spend AS (
    SELECT user_id,
           sum(amount_cents) AS total_cents,
           count(*)          AS n_orders
    FROM   orders
    GROUP  BY user_id
)
SELECT me.n_orders                                              AS order_count,
       me.total_cents                                           AS lifetime_cents,
       round(100.0 * count(*) FILTER (WHERE s.total_cents < me.total_cents)
             / nullif(count(*), 0), 1)                          AS spend_percentile
FROM   spend me
JOIN   spend s ON true
WHERE  me.user_id = $1
GROUP  BY me.n_orders, me.total_cents
"""

# Cheap: indexed, limited.
RECENT_ORDERS = """
SELECT id, item, amount_cents, placed_at
FROM   orders
WHERE  user_id = $1
ORDER  BY placed_at DESC
LIMIT  5
"""

# ── Episode 2: the write path ────────────────────────────────────────────────

# One column, one row. Cheap to write -- and it invalidates a cached object
# that cost 30-odd milliseconds of aggregation to build. That asymmetry is the
# whole reason invalidation is hard.
UPDATE_USER_CITY = """
UPDATE users
SET    city = $2
WHERE  id = $1
RETURNING city
"""

# Puts the demo user back to a known value so the race can be run again.
RESET_USER_CITY = """
UPDATE users
SET    city = $2
WHERE  id = $1
"""

# ── Episode 3: the batch warmer ──────────────────────────────────────────────
#
# The Avalanche story starts with a nightly job that fills the cache. A job
# that ran the per-user query 5,000 times would take minutes; a real one does
# what this does -- aggregates once, then writes many keys. The values it
# caches are identical to what the per-user path above produces, so the warmed
# cache is genuinely correct and not a stand-in.
#
# The percentile matches PROFILE_STATS exactly: rank() - 1 is the number of
# users who spent strictly less, over the same denominator.
BATCH_PROFILES = """
WITH spend AS (
    SELECT user_id,
           sum(amount_cents) AS total_cents,
           count(*)          AS n_orders
    FROM   orders
    GROUP  BY user_id
), ranked AS (
    SELECT user_id, total_cents, n_orders,
           round(100.0 * (rank() OVER (ORDER BY total_cents) - 1)
                 / count(*) OVER (), 1) AS spend_percentile
    FROM   spend
)
SELECT u.id, u.name, u.email, u.city, u.joined_at,
       r.n_orders        AS order_count,
       r.total_cents     AS lifetime_cents,
       r.spend_percentile
FROM   ranked r
JOIN   users u ON u.id = r.user_id
WHERE  u.id <= $1
ORDER  BY u.id
"""

# The five most recent orders for every user in the warm set, in one pass.
BATCH_RECENT_ORDERS = """
SELECT u.id AS user_id, o.id, o.item, o.amount_cents, o.placed_at
FROM   users u
JOIN   LATERAL (
           SELECT id, item, amount_cents, placed_at
           FROM   orders
           WHERE  user_id = u.id
           ORDER  BY placed_at DESC
           LIMIT  5
       ) o ON true
WHERE  u.id <= $1
"""

# Everything the Bloom filter needs to know.
ALL_USER_IDS = "SELECT id FROM users"

MAX_USER_ID = "SELECT max(id) AS max_id, count(*) AS n FROM users"
