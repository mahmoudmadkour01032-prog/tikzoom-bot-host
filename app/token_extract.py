"""Extract a Telegram bot token from a Python / PHP / Node.js source file."""
from __future__ import annotations

import re

# Telegram tokens look like: <digits 8-12>:<35-ish base64 chars>
TOKEN_RE = re.compile(r"\b(\d{8,12}:[A-Za-z0-9_\-]{30,})\b")


def extract_token(text: str) -> str | None:
    m = TOKEN_RE.search(text)
    return m.group(1) if m else None


def extract_token_from_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return extract_token(f.read())
    except OSError:
        return None
