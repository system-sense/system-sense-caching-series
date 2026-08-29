-- System Sense, Episode 1 -- seed data.
--
-- 100,000 users and 120,000 orders. The order count is tuned, not arbitrary:
-- the profile aggregate in app/queries.py has to cost what the episode's
-- latency table calls a "relational DB disk query", 10-50 ms. Small enough to
-- seed in seconds, big enough that the cost is real rather than staged.

CREATE TABLE users (
    id        BIGSERIAL PRIMARY KEY,
    name      TEXT        NOT NULL,
    email     TEXT        NOT NULL,
    city      TEXT        NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE orders (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT      NOT NULL REFERENCES users(id),
    item         TEXT        NOT NULL,
    amount_cents INTEGER     NOT NULL,
    placed_at    TIMESTAMPTZ NOT NULL
);

INSERT INTO users (name, email, city, joined_at)
SELECT
    (ARRAY['Ada','Grace','Alan','Linus','Barbara','Dennis','Radia','Ken',
           'Margaret','Tim','Katherine','Vint'])[1 + (i % 12)]
        || ' ' ||
    (ARRAY['Lovelace','Hopper','Turing','Torvalds','Liskov','Ritchie',
           'Perlman','Thompson','Hamilton','Berners-Lee','Johnson','Cerf'])[1 + (i % 12)],
    'user' || i || '@example.com',
    (ARRAY['Lisbon','Berlin','Toronto','Nairobi','Osaka','Bogota',
           'Dublin','Chennai','Oslo','Lima'])[1 + (i % 10)],
    now() - (i % 1500) * INTERVAL '1 day'
FROM generate_series(1, 100000) AS s(i);

-- Orders concentrate on the first 40,000 users, so the spend percentile
-- separates people instead of ranking everyone identically.
INSERT INTO orders (user_id, item, amount_cents, placed_at)
SELECT
    1 + (i % 40000),
    (ARRAY['Keyboard','Monitor','Desk lamp','Headphones','Mouse','Webcam',
           'Chair','Cable','Dock','Stand'])[1 + (i % 10)],
    (500 + (i::bigint * 7919) % 45000)::int,
    now() - (i % 900) * INTERVAL '1 day'
FROM generate_series(1, 120000) AS s(i);

CREATE INDEX orders_user_id_placed_at_idx ON orders (user_id, placed_at DESC);

-- Give the planner accurate statistics before the first request arrives, so the
-- demo measures a steady state rather than a cold-start artefact.
ANALYZE users;
ANALYZE orders;
