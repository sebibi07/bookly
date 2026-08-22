#!/usr/bin/env bash
# No-Docker runner: builds a local venv on first use, then starts the app.
#   ./run.sh          serve on http://127.0.0.1:8000
#   ./run.sh evals    conversation eval suite
#   ./run.sh wire     Anthropic request contract test
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  echo "  creating .venv …"
  "$PYTHON" -m venv .venv
fi
echo "  installing dependencies …"
./.venv/bin/pip install --quiet --disable-pip-version-check \
  -r requirements.txt -r requirements-dev.txt
exec ./.venv/bin/python run_local.py "$@"
