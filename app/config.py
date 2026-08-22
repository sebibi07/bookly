"""Runtime configuration. Everything has a working default so that
`docker compose up` succeeds with no .env file and no API key."""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- LLM -------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Two tiers, because the two jobs are not the same job.
#
# The agent writes what the customer reads, so it gets the better model. The
# router picks one of four labels against a fixed schema and has its own eval
# set -- which is exactly the shape of a task you can safely run on the
# cheapest, fastest model. Routing on the same tier as generation was doubling
# the latency of every turn to answer a four-way multiple-choice question.
MODEL = os.getenv("BOOKLY_MODEL", "claude-sonnet-5")
ROUTER_MODEL = os.getenv("BOOKLY_ROUTER_MODEL", "claude-haiku-4-5")

# Support conversations are short and heavily scaffolded; the hard decisions
# are already made in code before the model is called. Low effort keeps p50
# latency down without touching correctness.
EFFORT = os.getenv("BOOKLY_EFFORT", "low")
# Extended thinking is latency in a chat widget. Off by default here; set
# BOOKLY_THINKING=adaptive if you want it back.
THINKING = os.getenv("BOOKLY_THINKING", "off").strip().lower()
# The demo must never fail in front of an audience. With no key present we fall
# back to a deterministic scripted engine that implements the same interface.
USE_MOCK_LLM = _bool("BOOKLY_MOCK_LLM", not bool(ANTHROPIC_API_KEY))

# --- Identity --------------------------------------------------------------
JWT_SECRET = os.getenv("BOOKLY_JWT_SECRET", "dev-only-not-a-real-secret")
JWT_ISSUER = "bookly-identity"
JWT_AUDIENCE = "bookly-agent-tools"
# Short TTL: the token lives about as long as a support conversation. If a
# transcript leaks tomorrow, the token in it is already dead.
TOKEN_TTL_SECONDS = int(os.getenv("BOOKLY_TOKEN_TTL", "600"))
MAX_VERIFICATION_ATTEMPTS = int(os.getenv("BOOKLY_MAX_AUTH_ATTEMPTS", "3"))

# --- Business policy -------------------------------------------------------
# Encoded here, not in the prompt, so it is testable and cannot be argued with.
RETURN_WINDOW_DAYS = 30
DAMAGED_RETURN_WINDOW_DAYS = 90
# A parcel this far past its ETA stops being a status question and becomes a
# carrier trace, which a human owns.
LATE_PARCEL_ESCALATION_DAYS = 3

# --- Email -----------------------------------------------------------------
# With no SMTP host, mail is written to outbox/ as .eml and exposed at
# /api/outbox. The handoff works either way.
SMTP_HOST = os.getenv("BOOKLY_SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("BOOKLY_SMTP_PORT", "587"))
SMTP_USER = os.getenv("BOOKLY_SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("BOOKLY_SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("BOOKLY_EMAIL_FROM", "Bookly Support <support@bookly.example>")
# Redirect every outbound message to one address, the way a staging environment
# does. The body still names the account it was really for.
DEMO_EMAIL_OVERRIDE = os.getenv("BOOKLY_DEMO_EMAIL", "").strip()

# --- Database --------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://bookly:bookly@db:5432/bookly"
)

# --- Misc ------------------------------------------------------------------
MAX_TOOL_ITERATIONS = int(os.getenv("BOOKLY_MAX_TOOL_ITERATIONS", "6"))
