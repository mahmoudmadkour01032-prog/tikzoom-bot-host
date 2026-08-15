"""Broadcast a text message (optionally with an image) to every platform user.

Used by both the bot's ``/broadcast`` admin command and the Mini App admin tab.
The fan-out respects Telegram's ~30 messages/second flood limit by sleeping
between sends, and always swallows per-user errors so a single 403 (the user
blocked the bot) doesn't abort the whole run.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .repo import list_users
from .telegram_api import TelegramError, TgClient

logger = logging.getLogger(__name__)


@dataclass
class BroadcastReport:
    total: int = 0
    delivered: int = 0
    failed: int = 0
    blocked: int = 0
    skipped_banned: int = 0

    def as_html(self) -> str:
        return (
            "📣 <b>تقرير الإذاعة</b>\n\n"
            f"👥 الإجمالي: <b>{self.total}</b>\n"
            f"✅ تم التسليم: <b>{self.delivered}</b>\n"
            f"🚫 محظورون: <b>{self.skipped_banned}</b>\n"
            f"⛔️ حظروا البوت: <b>{self.blocked}</b>\n"
            f"❌ فشل: <b>{self.failed}</b>"
        )

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "delivered": self.delivered,
            "failed": self.failed,
            "blocked": self.blocked,
            "skipped_banned": self.skipped_banned,
        }


async def send_broadcast(
    client: TgClient,
    *,
    text: str | None = None,
    photo: str | None = None,
    skip_banned: bool = True,
    delay_ms: int = 40,
) -> BroadcastReport:
    """Send ``text`` (and optional ``photo`` path/file_id) to every user.

    - ``text``: HTML-formatted body. Required if no photo, otherwise becomes the caption.
    - ``photo``: optional. Either a Telegram ``file_id`` (preferred when broadcast
      came from the bot — Telegram caches & re-uses without re-uploading) or an
      absolute path on disk (used when broadcast came from the Mini App upload).
    - ``skip_banned``: skip users with ``is_banned=True``.
    - ``delay_ms``: pause between sends to stay under Telegram's flood limit.
    """
    if not text and not photo:
        raise ValueError("broadcast needs text or photo")
    report = BroadcastReport()
    users = await list_users(limit=100000)
    report.total = len(users)
    for u in users:
        if skip_banned and u.is_banned:
            report.skipped_banned += 1
            continue
        try:
            if photo:
                await client.send_photo(
                    u.user_id, photo,
                    caption=text or None, parse_mode="HTML",
                )
            else:
                await client.send_message(
                    u.user_id, text or "", parse_mode="HTML",
                )
            report.delivered += 1
        except TelegramError as exc:
            msg = str(exc).lower()
            if "blocked" in msg or "deactivated" in msg or "user is deactivated" in msg \
                    or "chat not found" in msg or "forbidden" in msg:
                report.blocked += 1
            else:
                report.failed += 1
                logger.warning("broadcast send to %s failed: %s", u.user_id, exc)
        except Exception as exc:  # noqa: BLE001
            report.failed += 1
            logger.warning("broadcast send to %s exception: %s", u.user_id, exc)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)
    return report
