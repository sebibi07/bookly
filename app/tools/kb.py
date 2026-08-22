"""Help-centre retrieval. No authentication required -- and that is a design
statement, not an oversight. 'What is your return window?' is answerable
without knowing who is asking, so we do not make the customer prove who they
are to get an answer."""
from app import db
from app.tools.base import Tool, ToolContext, obj


def search_help_center(ctx: ToolContext, query: str) -> dict:
    articles = db.search_kb(query)
    if not articles:
        return {
            "found": False,
            "note": "No help-centre article matched. Do not answer from memory; "
                    "offer to connect the customer to a human instead.",
        }
    return {
        "found": True,
        "articles": [
            {"slug": a["slug"], "title": a["title"], "content": a["body"]} for a in articles
        ],
        "note": "Answer only from the article text above and name the article you used.",
    }


TOOLS = [
    Tool(
        name="search_help_center",
        description=(
            "Search Bookly's published help-centre articles for policy questions: "
            "shipping times and costs, the returns and refunds policy, password "
            "resets, order changes, lost parcels. Use this for any general "
            "question that is not about one specific customer's order. You must "
            "call this before stating any Bookly policy."
        ),
        input_schema=obj(
            {"query": {"type": "string", "description": "The customer's question, in their own words."}},
            ["query"],
        ),
        handler=search_help_center,
        required_scope=None,
    )
]
