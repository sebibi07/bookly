"""Run Bookly without Docker.

Starts a real PostgreSQL server from the `pgserver` wheel (bundled binaries,
no system install, nothing left running afterwards) and points the app at it.
The application code path is identical to the Docker one -- same psycopg, same
schema, same everything. Only the way the database gets started differs.

    python run_local.py          # serve on http://127.0.0.1:8000
    python run_local.py evals    # conversation eval suite
    python run_local.py wire     # Anthropic request contract test
    python run_local.py smoke    # live conversation against the real API
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def load_dotenv() -> None:
    """Read .env, the way `docker compose` already does.

    Without this the two runners disagree about configuration, which is exactly
    the kind of difference that makes a demo behave one way in Docker and
    another way locally. Real environment variables win over the file.
    """
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def ensure_database() -> None:
    """Honour DATABASE_URL if set; otherwise bring up an embedded server."""
    if os.getenv("DATABASE_URL"):
        return
    try:
        import pgserver
    except ImportError:
        sys.exit(
            "Embedded Postgres is not installed.\n"
            "  pip install -r requirements-dev.txt\n"
            "…or point DATABASE_URL at any PostgreSQL instance, or use Docker."
        )
    data = HERE / ".pgdata"
    data.mkdir(exist_ok=True)
    os.environ["DATABASE_URL"] = pgserver.get_server(data).get_uri()


def main() -> int:
    sys.path.insert(0, str(HERE))
    load_dotenv()
    ensure_database()
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if command == "evals":
        from evals.run import main as run_evals
        return run_evals()
    if command == "wire":
        from evals.wire_check import main as run_wire
        return run_wire()
    if command == "smoke":
        from evals.smoke import main as run_smoke
        return run_smoke()
    if command != "serve":
        sys.exit(f"unknown command {command!r} — expected serve, evals, wire or smoke")

    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    print(f"\n  Bookly → http://127.0.0.1:{port}\n")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
