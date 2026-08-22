"""HTTP surface. Thin on purpose — all the interesting decisions live in
``orchestrator.py`` and ``tools/__init__.py``."""
import ipaddress
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config, db, notifications, orchestrator
from app.llm.anthropic_engine import AnthropicEngine
from app.llm.mock import MockEngine
from app.state import store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
)
log = logging.getLogger("bookly.api")

STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Compose starts us once Postgres reports healthy, but a healthy server is
    # not the same as an accepting one. Retry rather than crash-loop.
    for attempt in range(30):
        try:
            db.init_schema()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("waiting for database (%s/30): %s", attempt + 1, exc)
            time.sleep(1)
    else:
        raise RuntimeError("database never became available")

    log.info(
        "bookly agent ready | engine=%s agent=%s router=%s",
        "mock (no ANTHROPIC_API_KEY set)" if config.USE_MOCK_LLM else "anthropic",
        config.MODEL, config.ROUTER_MODEL,
    )
    yield


app = FastAPI(title="Bookly Support Agent", lifespan=lifespan)


@app.middleware("http")
async def no_store_assets(request: Request, call_next):
    """Never let a browser cache the demo's own HTML, CSS or JS.

    A cached app.js against fresh HTML is the worst kind of failure: the new
    button renders, nothing is wired to it, and there is no error to see. Not
    something to be debugging in front of an audience.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


@app.post("/api/chat")
def chat(req: ChatRequest):
    session = store.get_or_create(req.session_id)
    return orchestrator.handle_turn(session, req.message.strip())


@app.post("/api/reset")
def reset(req: ChatRequest | None = None, fixtures: bool = True):
    """Reset the conversation and, by default, the demo data.

    Returns are real writes, so running the damaged-book flow twice would
    otherwise hit "a return is already open on this order" the second time --
    correct behaviour, but confusing halfway through a recording. Pass
    ``?fixtures=false`` to keep whatever the database currently holds.
    """
    if fixtures:
        db.init_schema()
    session = store.reset(req.session_id) if req and req.session_id else store.get_or_create(None)
    return {"session_id": session.session_id, "fixtures_reseeded": fixtures}


@app.get("/api/session/{session_id}")
def session_detail(session_id: str):
    """Everything the demo might want to show: the trace, the auth state and
    the escalation packet handed to a human."""
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, "no such session")
    return {
        "session_id": session.session_id,
        "intent": session.intent,
        "auth": session.auth.public(),
        "escalated": session.escalated,
        "escalation": session.escalation,
        "trace": session.trace,
    }


class EngineRequest(BaseModel):
    # Write-only. This value is held in process memory for the life of the
    # server, is never written to disk, never logged, and never returned.
    api_key: str | None = None


def _is_local(request: Request) -> bool:
    """This endpoint accepts a credential, so it answers only to callers on the
    machine or its private network -- localhost, or the Docker bridge.

    The repository is public. Without this, anyone who deployed it to the open
    internet would be running an API-key collection form.
    """
    host = request.client.host if request.client else ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


@app.post("/api/engine")
def set_engine(req: EngineRequest, request: Request):
    """Switch between the live model and the scripted engine at runtime."""
    if not _is_local(request):
        raise HTTPException(403, "This endpoint is only available locally.")

    if not (req.api_key or "").strip():
        orchestrator.set_engine(MockEngine())
        return {"engine": "mock", "model": None}

    engine = AnthropicEngine(api_key=req.api_key.strip())
    try:
        engine.validate()
    except anthropic.AuthenticationError:
        raise HTTPException(400, "That API key was rejected by Anthropic.")
    except anthropic.PermissionDeniedError:
        raise HTTPException(400, f"That key cannot access {config.MODEL}.")
    except anthropic.NotFoundError:
        raise HTTPException(400, f"Model {config.MODEL} was not found for that key.")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Anthropic is rate limiting this key; try again shortly.")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach Anthropic. Check your connection.")
    except anthropic.APIStatusError as exc:
        # Deliberately does not echo the exception body, which can quote the request.
        raise HTTPException(502, f"Anthropic returned {exc.status_code}.")

    orchestrator.set_engine(engine)
    log.info("engine switched to anthropic (agent=%s router=%s) by local request",
             config.MODEL, config.ROUTER_MODEL)
    return {"engine": "anthropic", "model": config.MODEL, "router_model": config.ROUTER_MODEL}


@app.get("/api/outbox")
def read_outbox():
    """Everything the agent has mailed this run. Handy on a screen recording,
    and the .eml files under outbox/ open in any mail client."""
    sent = notifications.outbox().sent()
    return {
        "transport": notifications.outbox().name,
        "count": len(sent),
        "messages": [
            {
                "to": notifications.mask(m["to"]),
                "subject": m["subject"],
                "body": m["body"],
                "transport": m["transport"],
            }
            for m in reversed(sent)
        ],
    }


@app.get("/api/health")
def health(request: Request):
    return {
        "ok": True,
        "engine": orchestrator.engine().name,
        "model": config.MODEL,
        "router_model": config.ROUTER_MODEL,
        # Whether the browser should offer the key panel at all.
        "can_set_key": _is_local(request),
    }


def _asset_version() -> str:
    """Fingerprint the CSS and JS by modification time.

    `Cache-Control: no-store` should be enough, but browsers keep their own
    counsel and a half-cached page -- new markup, old stylesheet -- renders as
    an unstyled mess with no error anywhere. Changing the URL removes the
    browser's discretion entirely.
    """
    stamp = sum(
        (STATIC / name).stat().st_mtime_ns for name in ("styles.css", "app.js")
    )
    return f"{stamp % 100_000_000:08d}"


@app.get("/", response_class=HTMLResponse)
def index():
    version = _asset_version()
    html = (STATIC / "index.html").read_text()
    html = html.replace("/static/styles.css", f"/static/styles.css?v={version}")
    html = html.replace("/static/app.js", f"/static/app.js?v={version}")
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
