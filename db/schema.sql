-- Bookly support-agent demo schema.
-- Deliberately small: the point of the demo is the agent architecture,
-- not the data model.

DROP TABLE IF EXISTS returns, order_items, orders, customers, kb_articles CASCADE;

CREATE TABLE customers (
    id           SERIAL PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,
    full_name    TEXT NOT NULL,
    -- Second auth factor. Stored on the customer, not the order, so that a
    -- leaked order number alone is never sufficient to authenticate.
    shipping_zip TEXT NOT NULL
);

CREATE TABLE orders (
    id            SERIAL PRIMARY KEY,
    order_number  TEXT NOT NULL UNIQUE,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    status        TEXT NOT NULL,          -- processing | shipped | in_transit | delivered | delayed
    placed_at     DATE NOT NULL,
    shipped_at    DATE,
    delivered_at  DATE,
    carrier       TEXT,
    tracking_no   TEXT,
    eta           DATE,
    ship_zip      TEXT NOT NULL,
    total_cents   INTEGER NOT NULL
);

CREATE TABLE order_items (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    title       TEXT NOT NULL,
    author      TEXT NOT NULL,
    qty         INTEGER NOT NULL DEFAULT 1,
    price_cents INTEGER NOT NULL
);

CREATE TABLE returns (
    id           SERIAL PRIMARY KEY,
    rma          TEXT NOT NULL UNIQUE,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'approved',
    refund_cents INTEGER NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Help-centre content. Answering from this table (rather than from model
-- weights) is what makes policy answers auditable.
CREATE TABLE kb_articles (
    id      SERIAL PRIMARY KEY,
    slug    TEXT NOT NULL UNIQUE,
    title   TEXT NOT NULL,
    body    TEXT NOT NULL,
    tags    TEXT NOT NULL
);

CREATE INDEX ON orders (customer_id);
CREATE INDEX ON order_items (order_id);
