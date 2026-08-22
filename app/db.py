"""Thin Postgres access layer.

Every function that touches customer data takes an explicit ``customer_id``
that the caller must have obtained from verified token claims. There is no
convenience function that reads an order by number alone -- that absence is
the point.
"""
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from app import config

log = logging.getLogger("bookly.db")

_pool: ConnectionPool | None = None

# Every statement run while serving one turn, so the UI can show the actual
# query rather than a description of it. "0 rows" is a far better argument
# than "the agent declined".
QUERY_LOG: ContextVar[list | None] = ContextVar("bookly_query_log", default=None)


def start_query_log() -> list:
    entries: list = []
    QUERY_LOG.set(entries)
    return entries


def record(sql: str, params, rows: int, ms: float) -> None:
    entries = QUERY_LOG.get()
    if entries is None:
        return
    # Inline the parameters so the reader sees the query that actually ran.
    rendered = re.sub(r"\s+", " ", sql).strip()
    for value in (params or ()):
        literal = str(value) if isinstance(value, int) else f"'{value}'"
        rendered = rendered.replace("%s", literal, 1)
    entries.append({"sql": rendered, "rows": rows, "ms": round(ms, 1)})


class RecordingCursor:
    """Transparent wrapper that logs what it ran. Everything else passes
    straight through to psycopg."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=None):
        started = time.perf_counter()
        result = self._cur.execute(sql, params)
        record(sql, params, self._cur.rowcount, (time.perf_counter() - started) * 1000)
        return result

    def __getattr__(self, name):
        return getattr(self._cur, name)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8, open=True)
    return _pool


@contextmanager
def cursor():
    with pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield RecordingCursor(cur)


def init_schema() -> None:
    """Create and seed on boot. Idempotent because schema.sql drops first."""
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent / "db"
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute((here / "schema.sql").read_text())
            cur.execute((here / "seed.sql").read_text())
        conn.commit()
    log.info("schema created and seeded")


# --- Identity lookups ------------------------------------------------------

def find_customer_by_email(email: str) -> dict | None:
    with cursor() as cur:
        cur.execute(
            "SELECT id, email, full_name, shipping_zip FROM customers"
            " WHERE lower(email) = lower(%s)",
            (email.strip(),),
        )
        return cur.fetchone()


def get_customer(customer_id: int) -> dict | None:
    """Used to address outbound mail. The recipient comes from here -- never
    from anything said in the conversation."""
    with cursor() as cur:
        cur.execute(
            "SELECT id, email, full_name FROM customers WHERE id = %s", (customer_id,)
        )
        return cur.fetchone()


# --- Order lookups (always scoped by customer_id) --------------------------

def list_orders(customer_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT order_number, status, placed_at, delivered_at, eta, total_cents"
            "  FROM orders WHERE customer_id = %s ORDER BY placed_at DESC",
            (customer_id,),
        )
        return cur.fetchall()


def get_order(customer_id: int, order_number: str) -> dict | None:
    """Scoped read. An order belonging to somebody else returns None -- the
    caller reports it as 'not found on this account' rather than 'forbidden',
    so the endpoint cannot be used to enumerate valid order numbers."""
    with cursor() as cur:
        cur.execute(
            "SELECT id, order_number, status, placed_at, shipped_at, delivered_at,"
            "       carrier, tracking_no, eta, total_cents"
            "  FROM orders WHERE customer_id = %s AND upper(order_number) = upper(%s)",
            (customer_id, order_number.strip()),
        )
        order = cur.fetchone()
        if not order:
            return None
        cur.execute(
            "SELECT title, author, qty, price_cents FROM order_items WHERE order_id = %s",
            (order["id"],),
        )
        order["items"] = cur.fetchall()
        cur.execute("SELECT rma, status, reason FROM returns WHERE order_id = %s", (order["id"],))
        order["existing_returns"] = cur.fetchall()
        return order


def insert_return(order_id: int, rma: str, reason: str, refund_cents: int) -> dict:
    with pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            recording = RecordingCursor(cur)
            recording.execute(
                "INSERT INTO returns (rma, order_id, reason, refund_cents)"
                " VALUES (%s, %s, %s, %s) RETURNING rma, status, refund_cents",
                (rma, order_id, reason, refund_cents),
            )
            row = cur.fetchone()
        conn.commit()
    return row


# --- Help centre -----------------------------------------------------------

def search_kb(query: str, limit: int = 3) -> list[dict]:
    """Deliberately boring keyword search over a 5-row table.

    A vector index would be the production answer, but it would not change the
    architectural argument: policy answers are retrieved and cited, never
    recalled from model weights.
    """
    terms = [t for t in "".join(c if c.isalnum() else " " for c in query.lower()).split() if len(t) > 2]
    if not terms:
        return []
    with cursor() as cur:
        cur.execute("SELECT slug, title, body, tags FROM kb_articles")
        articles = cur.fetchall()
    scored = []
    for a in articles:
        haystack = f"{a['title']} {a['tags']} {a['body']}".lower()
        score = sum(haystack.count(t) for t in terms)
        # Tag hits are worth more than an incidental mention in body prose.
        score += 3 * sum(1 for t in terms if t in a["tags"].lower())
        if score:
            scored.append((score, a))
    scored.sort(key=lambda p: -p[0])
    return [a for _, a in scored[:limit]]
