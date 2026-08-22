"""Keyword heuristics shared by the scripted engine and the live router.

These live here rather than inside the mock because they do two jobs. They
drive the scripted engine, and they are the **fallback for the live router**:
if the classification call fails, the conversation routes on rules instead of
collapsing.

That matters more than it sounds. Routing decides which tools exist this turn,
so a router that fails open to "unclear" is a router that silently strips the
agent of every tool it needs. Degrading to keywords keeps the agent working;
degrading to "unclear" only looks like it does.
"""
import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Lookaround, not \b: "BK-10021" must not yield a ZIP of "10021".
ZIP_RE = re.compile(r"(?<![\w-])(\d{5})(?![\w-])")
ORDER_RE = re.compile(r"\bBK-?(\d{4,6})\b", re.I)

POLICY_WORDS = ("policy", "how long", "how much", "shipping cost", "password",
                "reset", "cancel", "change my address", "do you ship", "free shipping")
HUMAN_WORDS = ("human", "real person", "agent", "representative", "speak to someone", "manager")
RETURN_WORDS = ("return", "refund", "send it back", "money back", "exchange")
# A customer reporting a problem rarely uses the word "return" first.
DAMAGE_WORDS = ("damaged", "broken", "defective", "torn", "wrong book", "wrong item",
                "missing pages", "not what i ordered")
STATUS_WORDS = ("where", "status", "tracking", "arrive", "shipped", "delivery", "late", "when will")

REASON_HINTS = [
    (("damaged", "broken", "torn", "ripped", "crushed", "water"), "damaged"),
    (("defective", "misprint", "pages missing", "faulty"), "defective"),
    (("wrong", "not what i ordered", "different book"), "wrong_item"),
    (("changed my mind", "don't want", "do not want", "no longer need"), "changed_mind"),
    (("not as described", "misleading"), "not_as_described"),
]

INJECTION_MARKERS = (
    "ignore previous", "ignore your", "ignore all", "disregard", "system prompt",
    "your instructions", "developer mode", "pretend you", "you are now",
)




def user_texts(messages) -> list[str]:
    return [m["content"] for m in messages
            if m["role"] == "user" and isinstance(m["content"], str)]


def classify_intent(transcript) -> dict:
    """Route on keywords. Cheap, deterministic, and never returns `unclear`
    merely because something upstream broke."""
    texts = user_texts(transcript)
    last = (texts[-1] if texts else "").lower()
    prior = " ".join(texts[:-1]).lower()

    def hit(words, hay=last):
        return any(w in hay for w in words)

    if hit(RETURN_WORDS) or hit(DAMAGE_WORDS):
        intent, confidence = "return_refund", 0.92
    elif hit(STATUS_WORDS) and not hit(POLICY_WORDS):
        intent, confidence = "order_status", 0.9
    elif hit(POLICY_WORDS):
        intent, confidence = "policy_question", 0.88
    elif EMAIL_RE.search(last) or ZIP_RE.search(last) or ORDER_RE.search(last):
        # Answering a question we asked: keep whatever was already in play.
        if any(w in prior for w in RETURN_WORDS + DAMAGE_WORDS):
            intent, confidence = "return_refund", 0.85
        elif any(w in prior for w in STATUS_WORDS):
            intent, confidence = "order_status", 0.85
        else:
            intent, confidence = "unclear", 0.4
    else:
        intent, confidence = "unclear", 0.35

    return {"intent": intent, "confidence": confidence,
            "reasoning": "keyword routing", "latency_ms": 0.0, "usage": {}}
