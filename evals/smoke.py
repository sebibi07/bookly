"""Live smoke test. Requires a real ANTHROPIC_API_KEY.

Everything else in evals/ runs against the scripted engine or a stubbed
transport, which is what makes those suites fast, free and deterministic. It is
also their blind spot: a request the Anthropic API rejects looks perfectly
healthy to both of them.

That blind spot shipped a real bug. The classifier schema used
`minimum`/`maximum` on a number, which structured outputs reject with a 400.
Routing failed on every turn, the orchestrator fell back, and the agent
politely escalated every conversation while looking fine.

This runs a genuine conversation against the real API and asserts the thing
neither other suite can see: that nothing silently fell back.

    ./run.sh smoke
"""
import logging
import os
import sys

os.environ["BOOKLY_MOCK_LLM"] = "0"

from app import config, db, orchestrator  # noqa: E402
from app.state import Session  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

SCRIPT = [
    ("Where is my order?", {
        "intent": "order_status",
        "exposes": ["verify_customer"],
        "forbids": ["get_order_status"],
    }),
    ("sarah.chen@example.com, and the zip is 94110", {
        "calls": ["verify_customer"],
        "verified": True,
    }),
    ("BK-10021 please", {
        "calls": ["get_order_status"],
        "contains": ["1Z999AA10123456784"],
    }),
]


def main() -> int:
    if not config.ANTHROPIC_API_KEY:
        print(f"\n  {RED}ANTHROPIC_API_KEY is not set.{RESET} "
              "Put it in .env or export it, then rerun.\n")
        return 2

    db.init_schema()
    print(f"\n  Live smoke · agent={config.MODEL} router={config.ROUTER_MODEL}\n")

    session = Session()
    fails: list[str] = []

    for message, expect in SCRIPT:
        result = orchestrator.handle_turn(session, message)
        trace, reply = result["trace"], result["reply"]
        called = [c["tool"] for c in trace["tool_calls"]]

        print(f"  {DIM}›{RESET} {message}")
        print(f"    {reply[:150]}{'…' if len(reply) > 150 else ''}")
        print(f"    {DIM}{trace['intent']} {trace['intent_confidence']} · "
              f"{', '.join(called) or 'no tools'} · {trace['total_ms']}ms{RESET}\n")

        # The assertion that would have caught the shipped bug: any silent
        # fallback at all is a failure, however healthy the reply looks.
        if trace.get("llm_error"):
            fails.append(f"{message[:30]}…: fell back after {trace['llm_error']}")
        if trace["intent_confidence"] == 0.0:
            fails.append(f"{message[:30]}…: router returned no confidence")

        if "intent" in expect and trace["intent"] != expect["intent"]:
            fails.append(f"{message[:30]}…: intent {trace['intent']}, wanted {expect['intent']}")
        if "verified" in expect and trace["auth"]["verified"] != expect["verified"]:
            fails.append(f"{message[:30]}…: verified={trace['auth']['verified']}")
        for name in expect.get("calls", []):
            if name not in called:
                fails.append(f"{message[:30]}…: never called {name}")
        for name in expect.get("exposes", []):
            if name not in trace["tools_exposed"]:
                fails.append(f"{message[:30]}…: {name} was not exposed")
        for name in expect.get("forbids", []):
            if name in trace["tools_exposed"]:
                fails.append(f"{message[:30]}…: {name} must not be exposed yet")
        for needle in expect.get("contains", []):
            if needle.lower() not in reply.lower():
                fails.append(f"{message[:30]}…: reply missing {needle!r}")

    turns = session.trace
    print(f"  {DIM}median turn: "
          f"{sorted(t['total_ms'] for t in turns)[len(turns) // 2]:.0f}ms · "
          f"tokens in/out: {sum(t['usage']['input_tokens'] for t in turns)}/"
          f"{sum(t['usage']['output_tokens'] for t in turns)}{RESET}\n")

    if fails:
        for f in fails:
            print(f"  {RED}FAIL{RESET}  {f}")
        print(f"\n  {RED}{len(fails)} failed{RESET}\n")
        return 1
    print(f"  {GREEN}live smoke passed — nothing fell back{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
