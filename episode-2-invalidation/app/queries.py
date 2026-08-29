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
