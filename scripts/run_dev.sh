#!/usr/bin/env bash
# Local dev runner — uses polling so no public HTTPS is needed.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -e ".[dev]"

if [[ -z "${FERNET_KEY:-}" ]]; then
  export FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  echo "Generated dev FERNET_KEY (set env var to keep stable)."
fi

export TIKZOOM_POLL=1
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}" --reload
