"""Reference webhook bot template (Python).

Drop your own logic into `handle_update`. The platform sets:
  BOT_TOKEN     — your Telegram token
  PORT          — local TCP port to bind on (loopback)
  WEBHOOK_PATH  — path the platform forwards updates to
"""
from __future__ import annotations

import json
import os

import httpx
import uvicorn
from fastapi import FastAPI, Request

TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "0") or 0) or 5000
PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
TG = f"https://api.telegram.org/bot{TOKEN}"

app = FastAPI()


async def handle_update(update: dict, http: httpx.AsyncClient) -> None:
    msg = update.get("message")
    if msg and msg.get("text"):
        chat_id = msg["chat"]["id"]
        await http.post(f"{TG}/sendMessage", data={
            "chat_id": chat_id,
            "text": "👋 أهلاً! أنا بوتك المُستضاف على TikZoom.",
        })


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    update = await request.json()
    async with httpx.AsyncClient(timeout=10.0) as http:
        await handle_update(update, http)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
