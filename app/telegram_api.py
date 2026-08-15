"""Tiny async wrapper for the Telegram Bot API.

We talk to the Bot API directly via httpx so we can pass the new Bot API 9.4
fields (`style`, `icon_custom_emoji_id`) on buttons even when the high-level
library hasn't yet exposed them.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


class TgClient:
    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TgClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def call(self, method: str, **params: Any) -> Any:
        url = API_BASE.format(token=self.token, method=method)
        # Some fields like reply_markup must be JSON-encoded.
        clean: dict = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                clean[k] = json.dumps(v, ensure_ascii=False)
            else:
                clean[k] = v
        try:
            r = await self._client.post(url, data=clean)
        except httpx.HTTPError as exc:
            raise TelegramError(f"network error: {exc}") from exc
        try:
            payload = r.json()
        except Exception:
            raise TelegramError(f"non-json response: {r.text[:200]}") from None
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload.get('description')}")
        return payload.get("result")

    async def send_message(self, chat_id: int | str, text: str, *, parse_mode: str | None = "HTML",
                           reply_markup: dict | None = None, **kw: Any) -> Any:
        return await self.call("sendMessage", chat_id=chat_id, text=text,
                               parse_mode=parse_mode, reply_markup=reply_markup, **kw)

    async def edit_message_text(self, chat_id: int | str, message_id: int, text: str, *,
                                parse_mode: str | None = "HTML",
                                reply_markup: dict | None = None, **kw: Any) -> Any:
        return await self.call("editMessageText", chat_id=chat_id, message_id=message_id,
                               text=text, parse_mode=parse_mode, reply_markup=reply_markup, **kw)

    async def answer_callback_query(self, callback_query_id: str, *, text: str | None = None,
                                    show_alert: bool = False) -> Any:
        return await self.call("answerCallbackQuery", callback_query_id=callback_query_id,
                               text=text, show_alert=show_alert)

    async def send_document(self, chat_id: int | str, document_path: str, *,
                            caption: str | None = None,
                            parse_mode: str | None = "HTML") -> Any:
        url = API_BASE.format(token=self.token, method="sendDocument")
        files = {"document": open(document_path, "rb")}
        try:
            data: dict = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            if parse_mode:
                data["parse_mode"] = parse_mode
            r = await self._client.post(url, data=data, files=files)
        finally:
            files["document"].close()
        payload = r.json()
        if not payload.get("ok"):
            raise TelegramError(f"sendDocument: {payload.get('description')}")
        return payload.get("result")

    async def send_photo(self, chat_id: int | str, photo: str, *,
                         caption: str | None = None,
                         parse_mode: str | None = "HTML",
                         reply_markup: dict | None = None) -> Any:
        """Send a photo. ``photo`` may be either a Telegram ``file_id`` (re-use)
        or an absolute path to a local file (uploaded as multipart)."""
        from pathlib import Path as _P
        if photo.startswith(("http://", "https://")) or not _P(photo).is_file():
            return await self.call("sendPhoto", chat_id=chat_id, photo=photo,
                                   caption=caption, parse_mode=parse_mode,
                                   reply_markup=reply_markup)
        url = API_BASE.format(token=self.token, method="sendPhoto")
        files = {"photo": open(photo, "rb")}
        try:
            data: dict = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            if parse_mode:
                data["parse_mode"] = parse_mode
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            r = await self._client.post(url, data=data, files=files)
        finally:
            files["photo"].close()
        payload = r.json()
        if not payload.get("ok"):
            raise TelegramError(f"sendPhoto: {payload.get('description')}")
        return payload.get("result")

    async def get_file(self, file_id: str) -> dict:
        return await self.call("getFile", file_id=file_id)

    async def download_file(self, file_path: str) -> bytes:
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        r = await self._client.get(url)
        r.raise_for_status()
        return r.content

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> dict:
        return await self.call("getChatMember", chat_id=chat_id, user_id=user_id)

    async def set_webhook(self, url: str, *, secret_token: str | None = None,
                          drop_pending_updates: bool = True,
                          allowed_updates: list[str] | None = None,
                          certificate_path: str | None = None) -> Any:
        if certificate_path:
            files = {"certificate": open(certificate_path, "rb")}
            data: dict = {"url": url, "drop_pending_updates": "true" if drop_pending_updates else "false"}
            if secret_token:
                data["secret_token"] = secret_token
            if allowed_updates:
                data["allowed_updates"] = json.dumps(allowed_updates)
            try:
                r = await self._client.post(API_BASE.format(token=self.token, method="setWebhook"),
                                            data=data, files=files)
            finally:
                files["certificate"].close()
            payload = r.json()
            if not payload.get("ok"):
                raise TelegramError(f"setWebhook: {payload.get('description')}")
            return payload.get("result")
        return await self.call("setWebhook", url=url, secret_token=secret_token,
                               drop_pending_updates=drop_pending_updates,
                               allowed_updates=allowed_updates)

    async def delete_webhook(self, drop_pending_updates: bool = True) -> Any:
        return await self.call("deleteWebhook", drop_pending_updates=drop_pending_updates)

    async def get_webhook_info(self) -> dict:
        return await self.call("getWebhookInfo")


async def validate_token(token: str) -> dict | None:
    """Returns getMe payload on success, None on failure."""
    try:
        async with TgClient(token, timeout=10.0) as cli:
            return await cli.get_me()
    except Exception as exc:
        logger.info("token validation failed: %s", exc)
        return None
