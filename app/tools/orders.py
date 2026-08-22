"""Order tools. Every read is scoped to the ``sub`` claim of the caller's
token; ``order_number`` is a filter within that customer's own orders, never a
lookup key on its own."""
from datetime import date

from app import config, db
from app.identity import customer_id_from
from app.tools.base import Tool, ToolContext, money, obj

STATUS_PHRASING = {
    "processing": "being picked and packed in our warehouse",
    "shipped": "handed to the carrier",
    "in_transit": "moving through the carrier network",
    "delivered": "delivered",
    "delayed": "running late — the carrier has not scanned it recently",
}


def _summarise(order: dict) -> dict:
    days_late = None
    if order["eta"] and not order["delivered_at"]:
        days_late = max(0, (date.today() - order["eta"]).days)
    return {
        "order_number": order["order_number"],
        "status": order["status"],
        "status_in_plain_english": STATUS_PHRASING.get(order["status"], order["status"]),
        "placed_on": str(order["placed_at"]),
        "shipped_on": str(order["shipped_at"]) if order["shipped_at"] else None,
        "delivered_on": str(order["delivered_at"]) if order["delivered_at"] else None,
        "estimated_delivery": str(order["eta"]) if order["eta"] else None,
        "days_past_estimated_delivery": days_late,
        "carrier": order["carrier"],
        "tracking_number": order["tracking_no"],
        "order_total": money(order["total_cents"]),
        "items": [f"{i['qty']}x {i['title']} by {i['author']}" for i in order["items"]],
        # The agent is told when a situation has outgrown its tools rather than
        # being left to work it out from the raw dates.
        "requires_human_review": bool(
            days_late is not None and days_late > config.LATE_PARCEL_ESCALATION_DAYS
        ),
        "human_review_reason": (
            "Parcel is more than "
            f"{config.LATE_PARCEL_ESCALATION_DAYS} days past its estimated delivery date. "
            "Policy requires a carrier trace, which only a human agent can open. "
            "Do not promise a delivery date. Escalate."
            if days_late is not None and days_late > config.LATE_PARCEL_ESCALATION_DAYS
            else None
        ),
    }


def list_my_orders(ctx: ToolContext) -> dict:
    customer_id = customer_id_from(ctx.token, "orders:read")
    orders = db.list_orders(customer_id)
    open_orders = [o for o in orders if o["status"] != "delivered"]
    return {
        "order_count": len(orders),
        "open_order_count": len(open_orders),
        "orders": [
            {
                "order_number": o["order_number"],
                "status": o["status"],
                "placed_on": str(o["placed_at"]),
                "order_total": money(o["total_cents"]),
            }
            for o in orders
        ],
        "note": (
            "This customer has more than one order. Ask which one they mean "
            "before giving a status — do not assume the most recent."
            if len(orders) > 1
            else None
        ),
    }


def get_order_status(ctx: ToolContext, order_number: str) -> dict:
    customer_id = customer_id_from(ctx.token, "orders:read")
    order = db.get_order(customer_id, order_number)
    if order is None:
        # Deliberately indistinguishable from "that order does not exist".
        return {
            "found": False,
            "note": (
                f"No order {order_number} on this account. Tell the customer you "
                "cannot see it on their account and offer to list their orders. "
                "Do not speculate about who it might belong to."
            ),
        }
    return {"found": True, **_summarise(order)}


TOOLS = [
    Tool(
        name="list_my_orders",
        description=(
            "List every order on the verified customer's account, newest first. "
            "Call this when the customer refers to 'my order' without naming one, "
            "so you can ask which order they mean if there is more than one."
        ),
        input_schema=obj({}, []),
        handler=list_my_orders,
        required_scope="orders:read",
        tags=["orders"],
    ),
    Tool(
        name="get_order_status",
        description=(
            "Get the full shipping status, carrier, tracking number and contents "
            "of one specific order belonging to the verified customer."
        ),
        input_schema=obj(
            {"order_number": {"type": "string", "description": "e.g. BK-10021"}},
            ["order_number"],
        ),
        handler=get_order_status,
        required_scope="orders:read",
        tags=["orders"],
    ),
]
