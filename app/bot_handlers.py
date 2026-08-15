"""Update dispatcher for the main platform bot.

We process Telegram updates received via webhook (POST /tg/<secret>) by routing
each update to the right handler. Designed to be light: no aiogram needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import html
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .config import get_settings
from .ai_assistant import (
    MCV_SYSTEM_PROMPT_AR,
    MCVError,
    detect_bot_purpose,
    generate_bot,
    is_done_phrase,
    is_exit_phrase,
    modify_bot_code,
    project_analyze,
    transpile_to_python,
    wizard_acknowledge,
)
from .db import HostedBot
from .keyboards import Btn, inline_kb, kb_back_main, kb_force_sub, kb_main_menu, kb_share_contact
from .locales import t
from .notifications import notify_admins_upload
from .repo import (
    add_force_sub_channel,
    add_hosted_bot,
    audit,
    count_referrals,
    count_user_bots_in_tier,
    credit_referral,
    delete_bot,
    get_bot,
    get_setting,
    get_user,
    get_user_by_referral_code,
    list_force_sub_channels,
    list_user_bots,
    remove_force_sub_channel,
    set_admin,
    set_banned,
    set_contact,
    set_force_sub_verified,
    set_setting,
    set_vip,
    upsert_user,
)
from .deps import install_dependencies
from .runner import allocate_port, detect_language, get_runner
from .security import encrypt_token, token_hash
from .telegram_api import TelegramError, TgClient, validate_token
from .tiers import TIERS, by_level, can_use_tier, max_files_for, unlocked_tiers
from .token_extract import extract_token_from_file

logger = logging.getLogger(__name__)


# ----- in-memory pending state (per user, expires after one action) ----- #
_pending: dict[int, dict[str, Any]] = {}
_pending_lock = asyncio.Lock()


async def set_pending(uid: int, data: dict[str, Any]) -> None:
    async with _pending_lock:
        _pending[uid] = data


async def pop_pending(uid: int) -> dict[str, Any] | None:
    async with _pending_lock:
        return _pending.pop(uid, None)


async def get_pending(uid: int) -> dict[str, Any] | None:
    async with _pending_lock:
        return _pending.get(uid)


# ----- Helpers ----- #

async def is_admin_uid(uid: int) -> bool:
    s = get_settings()
    if uid in s.admin_id_list:
        return True
    u = await get_user(uid)
    return bool(u and u.is_admin)


async def check_force_subs(client: TgClient, uid: int) -> tuple[bool, list[tuple[int, str | None, str | None]]]:
    channels = await list_force_sub_channels()
    if not channels:
        return True, []
    missing: list[tuple[int, str | None, str | None]] = []
    for ch in channels:
        try:
            mem = await client.get_chat_member(ch.chat_id, uid)
            if mem.get("status") not in ("member", "administrator", "creator"):
                missing.append((ch.chat_id, ch.title, ch.invite_link))
        except TelegramError as exc:
            logger.info("force-sub check %s/%s failed: %s", ch.chat_id, uid, exc)
            missing.append((ch.chat_id, ch.title, ch.invite_link))
    return (not missing), missing


async def public_base_url() -> str:
    """Public base URL for the platform.

    Resolution order:
    1. Admin-set override stored in DB settings (`public_base_url`).
    2. Env var ``PUBLIC_BASE_URL``.
    3. Auto-detected ``https://*.trycloudflare.com`` URL from the
       cloudflared log under ``{data_dir}/logs/cloudflared.log``.
    """
    override = await get_setting("public_base_url", "")
    if override:
        return override.rstrip("/")
    env_url = get_settings().public_base_url
    if env_url and env_url not in ("https://localhost",):
        return env_url.rstrip("/")
    return _read_cloudflared_url() or env_url.rstrip("/")


_TRYCF_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _read_cloudflared_url() -> str:
    """Scan cloudflared.log for the most-recent trycloudflare URL."""
    log = get_settings().data_path / "logs" / "cloudflared.log"
    if not log.exists():
        return ""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    matches = _TRYCF_RE.findall(text)
    return matches[-1] if matches else ""


def webhook_url_for_token(base: str, tk_hash: str) -> str:
    return f"{base.rstrip('/')}/wh/{tk_hash}"


# Heuristics for detecting whether a hosted bot was written for polling or
# webhook mode by reading its source file.
#
# Polling is the safer default for the majority of community bots:
#  * It does not require a public HTTPS endpoint to be reachable.
#  * It does not crash with a 409 conflict if a stale webhook is registered
#    (the platform always calls ``deleteWebhook`` before launch).
#  * Most ``python-telegram-bot`` / ``pyTelegramBotAPI`` (telebot) /
#    ``aiogram`` examples that beginners upload run polling.
#
# We detect webhook mode by looking for explicit webhook-server markers in
# the source. Anything else falls through to polling.
_WEBHOOK_MARKERS: tuple[str, ...] = (
    "process_webhook",
    "process_new_updates",
    "register_blueprint",
    "fastapi(",
    "uvicorn.run",
    "flask(",
    "create_app(",
    "app.run(",
    "express(",
    "http.createserver",
    "set_webhook(",
    "setwebhook",
    "webhook_handler",
)


def detect_run_mode(file_path: str, language: str) -> str:
    """Return ``"polling"`` or ``"webhook"`` based on a quick source scan.

    We only scan the entry file (not the whole upload tree), which is good
    enough for the typical single-file bot uploads the platform receives.
    Polling is the default if the file is unreadable or no markers match,
    because it works in firewalled environments and is what most uploaded
    bots use.
    """
    # PHP files are virtually always webhook (Apache/CLI-served scripts).
    if language == "php":
        return "webhook"
    try:
        src = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError, ValueError):
        try:
            src = Path(file_path).read_bytes().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            src = ""
    lower = src.lower()
    if not lower:
        return "polling"
    # Polling markers first — if we see any of these the bot definitely
    # wants polling, so don't bother checking webhook markers.
    polling_markers = (
        "infinity_polling", "start_polling", "run_polling", "polling_loop",
        "long_polling", "longpolling", "polling()", "getupdates",
        "get_updates", "polling: true",
    )
    for marker in polling_markers:
        if marker in lower:
            return "polling"
    for marker in _WEBHOOK_MARKERS:
        if marker in lower:
            return "webhook"
    return "polling"


async def webapp_url_for_user() -> str | None:
    base = await public_base_url()
    if base.startswith("https://"):
        # Cache-buster: bump every minute so Telegram refetches the page
        # whenever a new build is deployed.
        version = dt.datetime.utcnow().strftime("%Y%m%d%H%M")
        return f"{base.rstrip('/')}/app/?v={version}"
    return None


async def main_menu_text(uid: int, lang: str) -> str:
    u = await get_user(uid)
    points = u.points if u else 0
    refs = await count_referrals(uid)
    is_admin = await is_admin_uid(uid)
    is_vip = bool(u and u.is_vip)
    tiers = unlocked_tiers(points, is_vip=is_vip, is_admin=is_admin)
    tier_label = tiers[-1].label_ar if lang == "ar" else tiers[-1].label_en if tiers else "—"
    # Optional admin announcement shown as a quoted blockquote at the top.
    announcement = (await get_setting("welcome_announcement", "")).strip()
    pieces: list[str] = []
    if announcement:
        # The announcement is plain text from an admin; HTML-escape it so
        # any stray ``<``/``>`` doesn't break the parse.
        ann_html = html.escape(announcement, quote=False).replace("\n", "\n")
        pieces.append(f"<blockquote>{ann_html}</blockquote>")
    quote_body = (
        f"<blockquote><b>{t(lang, 'welcome_header').replace('<b>', '').replace('</b>', '')}</b>\n"
        f"{t(lang, 'developed_by')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"💎 {t(lang, 'your_points', points=points)}\n"
        f"👥 {t(lang, 'your_referrals', count=refs)}\n"
        f"🚀 {t(lang, 'your_tier', tier=html.escape(tier_label, quote=False))}</blockquote>"
    )
    pieces.append(quote_body)
    return "\n".join(pieces)


async def show_main_menu(client: TgClient, chat_id: int, uid: int, lang: str,
                         *, edit_message_id: int | None = None) -> None:
    text = await main_menu_text(uid, lang)
    is_admin = await is_admin_uid(uid)
    web_app_url = await webapp_url_for_user()
    kb = kb_main_menu(lang, is_admin=is_admin, web_app_url=web_app_url)
    if edit_message_id is not None:
        try:
            await client.edit_message_text(chat_id, edit_message_id, text, reply_markup=kb)
            return
        except TelegramError:
            pass
    await client.send_message(chat_id, text, reply_markup=kb)


# ----- Top-level update entrypoint ----- #

async def handle_update(client: TgClient, update: dict) -> None:
    try:
        if "message" in update:
            await _handle_message(client, update["message"])
        elif "callback_query" in update:
            await _handle_callback(client, update["callback_query"])
    except Exception:  # noqa: BLE001
        logger.exception("update handler failed")


async def _handle_message(client: TgClient, msg: dict) -> None:
    user = msg.get("from") or {}
    uid: int = int(user.get("id", 0))
    if not uid:
        return
    chat_id = msg["chat"]["id"]
    lang = (user.get("language_code") or "ar")[:2]
    if lang not in ("ar", "en"):
        lang = "ar"
    u = await upsert_user(
        user_id=uid,
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
        language=lang,
    )
    if u.is_banned:
        await client.send_message(chat_id, t(u.language or lang, "banned"))
        return
    lang = u.language or lang

    text = msg.get("text", "") or ""
    contact = msg.get("contact")
    document = msg.get("document")

    # 1) Contact share?
    if contact:
        await set_contact(uid, contact.get("phone_number") or "")
        await client.send_message(chat_id, t(lang, "contact_saved"))
        await show_main_menu(client, chat_id, uid, lang)
        return

    # 2) /start command
    if text.startswith("/start"):
        await _cmd_start(client, msg, u, lang)
        return

    if text.startswith("/help") or text == "❓":
        await client.send_message(chat_id, await main_menu_text(uid, lang))
        return

    if text.startswith("/admin"):
        if not await is_admin_uid(uid):
            await client.send_message(chat_id, t(lang, "admin_only"))
            return
        await _show_admin_panel(client, chat_id, lang)
        return

    if text.startswith("/setbase"):
        # Admin sets public base URL: /setbase https://example.com
        if not await is_admin_uid(uid):
            return
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            await set_setting("public_base_url", parts[1].strip())
            await client.send_message(chat_id, f"✅ تم تعيين الرابط: `{parts[1].strip()}`")
        return

    if text.startswith("/broadcast"):
        if not await is_admin_uid(uid):
            await client.send_message(chat_id, t(lang, "admin_only"))
            return
        # Two ways to invoke:
        #   /broadcast <text>            → text-only broadcast
        #   /broadcast (no args)         → enters "send me a photo or text" mode
        body = text[len("/broadcast"):].strip()
        if body:
            from .broadcast import send_broadcast
            await client.send_message(
                chat_id, "📡 بدأت الإذاعة... هتبعتلك التقرير لما تخلص.",
            )
            report = await send_broadcast(client, text=body)
            await client.send_message(chat_id, report.as_html())
            return
        await set_pending(uid, {"kind": "admin_broadcast"})
        await client.send_message(
            chat_id,
            "📣 <b>وضع الإذاعة</b>\n\n"
            "ابعتلي رسالة أو صورة (مع أو بدون تعليق) وهتتبعت لكل المستخدمين.\n\n"
            "ابعت <code>/cancel</code> للإلغاء.",
        )
        return

    if text.startswith("/cancel"):
        await pop_pending(uid)
        await client.send_message(chat_id, "✖️ تم الإلغاء.")
        return

    # 3) Pending interactions (uploading, awaiting input from admin, etc.)
    pending = await get_pending(uid)

    if document and pending and pending.get("kind") == "upload_file":
        await _process_upload(client, msg, u, lang, tier_level=pending.get("tier", 1))
        # Only clear the slot if ``_process_upload`` didn't already replace it
        # (e.g. with ``upload_choose_mode`` while waiting for the user to pick
        # Polling or Webhook). Otherwise we'd wipe the new state immediately
        # and the next callback would see "session ended".
        cur = await get_pending(uid)
        if not cur or cur.get("kind") == "upload_file":
            await pop_pending(uid)
        return

    if document and pending and pending.get("kind") == "ai_project_upload":
        await _process_ai_project_upload(client, msg, u, lang)
        cur = await get_pending(uid)
        if not cur or cur.get("kind") == "ai_project_upload":
            await pop_pending(uid)
        return

    if pending and pending.get("kind") == "admin_set_main_token" and text:
        await _admin_apply_main_token(client, chat_id, lang, text.strip())
        await pop_pending(uid)
        return

    if pending and pending.get("kind") == "admin_add_channel" and text:
        await _admin_apply_add_channel(client, chat_id, lang, text.strip())
        await pop_pending(uid)
        return

    if pending and pending.get("kind") == "admin_set_user_role" and text:
        await _admin_apply_user_role(client, chat_id, lang, text.strip(), pending.get("op", ""))
        await pop_pending(uid)
        return

    if pending and pending.get("kind") == "admin_broadcast":
        # Admin's next message is the broadcast payload (photo or text).
        from .broadcast import send_broadcast
        photo_path: str | None = None
        caption_text: str | None = None
        # Caption ships in msg["caption"] for a photo upload, or msg["text"] otherwise.
        if msg.get("photo"):
            sizes = msg["photo"]
            largest = max(sizes, key=lambda s: int(s.get("file_size") or 0))
            # Telegram lets us re-send by file_id, no re-upload necessary.
            photo_path = largest["file_id"]
            caption_text = msg.get("caption") or None
        elif text:
            caption_text = text
        else:
            await client.send_message(chat_id, "❌ ابعت رسالة نصية أو صورة.")
            return
        await pop_pending(uid)
        await client.send_message(chat_id, "📡 بدأت الإذاعة... استنى التقرير.")
        report = await send_broadcast(client, text=caption_text, photo=photo_path)
        await client.send_message(chat_id, report.as_html())
        return

    if pending and pending.get("kind") == "admin_set_announcement" and text:
        new_val = text.strip()
        if new_val == "-":
            await set_setting("welcome_announcement", "")
            await client.send_message(chat_id, "🗑️ تم مسح الإعلان.")
        else:
            await set_setting("welcome_announcement", new_val)
            await client.send_message(chat_id,
                f"📣 <b>تم حفظ الإعلان:</b>\n"
                f"<blockquote>{html.escape(new_val, quote=False)}</blockquote>")
        await pop_pending(uid)
        await show_main_menu(client, chat_id, uid, lang)
        return

    # ----- MCV pending flows ----- #
    if pending and pending.get("kind") == "mcv_chat" and text:
        # The chat stays open across many turns. The user has to type
        # "خروج" (or one of the other exit phrases) to leave on purpose.
        if is_exit_phrase(text):
            await pop_pending(uid)
            await client.send_message(
                chat_id,
                "👋 خرجنا من وضع الكلام. لو احتجتني تاني اضغط 🔴 MCV.",
                reply_markup=kb_back_main(lang),
            )
            return
        await _mcv_continue_chat(client, chat_id, uid, lang, text, pending)
        return

    if pending and pending.get("kind") == "mcv_make_bot" and text:
        # Free-form chat in *code mode*: every reply may emit a Python
        # file, which is sent as a document with Run/Save/Cancel buttons.
        if is_exit_phrase(text):
            await pop_pending(uid)
            await client.send_message(
                chat_id,
                "👋 خرجنا من وضع صناعة البوتات. لو احتجتني تاني اضغط 🔴 MCV.",
                reply_markup=kb_back_main(lang),
            )
            return
        await _mcv_make_bot_turn(client, chat_id, uid, lang, text, pending, u)
        return

    if pending and pending.get("kind") == "mcv_wizard" and text:
        await _mcv_wizard_step(client, chat_id, uid, lang, text.strip(), pending)
        return

    if (pending and pending.get("kind") == "mcv_await_run"
            and pending.get("stage") == "await_edit_prompt" and text):
        # User clicked "✏️ عدّل قبل التشغيل" and is now describing the
        # change. Apply it to the drafted file and re-prompt for run.
        await _mcv_edit_drafted_file(
            client, chat_id, uid, lang,
            edit_request=text.strip(), pending=pending,
        )
        return

    if pending and pending.get("kind") == "mcv_edit_bot" and text:
        bot_id = pending.get("bot_id")
        await pop_pending(uid)
        if bot_id:
            await _mcv_edit_existing_bot(client, chat_id, uid, lang,
                                        bot_id=int(bot_id), instructions=text.strip())
        return

    if pending and pending.get("kind") == "change_bot_token" and text:
        bot_id = pending.get("bot_id")
        await pop_pending(uid)
        if bot_id:
            await _apply_bot_token_change(client, chat_id, uid, lang,
                                         bot_id=int(bot_id), new_token=text.strip())
        return

    if pending and pending.get("kind") == "mcv_new_bot_await_token" and text:
        await pop_pending(uid)
        await _finalize_mcv_new_bot(client, chat_id, uid, lang,
                                     draft_path=pending["draft_path"],
                                     name=pending["name"],
                                     token=text.strip())
        return

    if pending and pending.get("kind") == "ai_project_await_token" and text:
        await pop_pending(uid)
        await _finalize_ai_project(
            client, chat_id, uid, lang,
            main_file=pending["main_file"],
            sub_dir=pending["sub_dir"],
            language=pending["language"],
            token=text.strip(),
            run_mode=pending.get("run_mode", "polling"),
            project_label=pending.get("project_label", "MCV project"),
        )
        return

    # Default — show menu
    await show_main_menu(client, chat_id, uid, lang)


async def _cmd_start(client: TgClient, msg: dict, u, lang: str) -> None:
    chat_id = msg["chat"]["id"]
    uid = u.user_id

    # Parse optional referral code: /start <code>
    text = msg.get("text", "")
    parts = text.split(maxsplit=1)
    ref_code = parts[1].strip() if len(parts) == 2 else ""
    if ref_code:
        ref_user = await get_user_by_referral_code(ref_code)
        if ref_user and ref_user.user_id != uid:
            credited = await credit_referral(referrer_id=ref_user.user_id, referred_id=uid)
            if credited:
                with contextlib.suppress(Exception):
                    await client.send_message(ref_user.user_id, t(ref_user.language or "ar", "referral_credited"))
                await audit(uid, "referral_credited", f"by={ref_user.user_id}")

    # Contact gate
    if not u.contact_phone:
        kb = kb_share_contact(lang)
        await client.send_message(chat_id, t(lang, "share_contact_required"), reply_markup=kb)
        return

    # Force-subscribe gate
    ok, missing = await check_force_subs(client, uid)
    if not ok:
        kb = kb_force_sub(missing, lang)
        await client.send_message(chat_id, t(lang, "force_sub_required"), reply_markup=kb)
        return
    await set_force_sub_verified(uid)
    await show_main_menu(client, chat_id, uid, lang)


# ----- Callback queries ----- #

async def _handle_callback(client: TgClient, cb: dict) -> None:
    user = cb.get("from") or {}
    uid: int = int(user.get("id", 0))
    if not uid:
        return
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    data = cb.get("data") or ""

    u = await get_user(uid) or await upsert_user(
        user_id=uid, username=user.get("username"),
        first_name=user.get("first_name"), last_name=user.get("last_name"),
    )
    if u.is_banned:
        await client.answer_callback_query(cb["id"], text="🚫 محظور", show_alert=True)
        return
    lang = u.language or "ar"

    async def ack(text: str | None = None, show_alert: bool = False) -> None:
        with contextlib.suppress(Exception):
            await client.answer_callback_query(cb["id"], text=text, show_alert=show_alert)

    if data == "main":
        await ack()
        await show_main_menu(client, chat_id, uid, lang, edit_message_id=msg_id)
        return

    if data == "check_force_sub":
        ok, missing = await check_force_subs(client, uid)
        if ok:
            await set_force_sub_verified(uid)
            await ack(t(lang, "force_sub_ok"))
            await show_main_menu(client, chat_id, uid, lang, edit_message_id=msg_id)
        else:
            await ack(t(lang, "force_sub_fail"), show_alert=True)
        return

    if data == "developer":
        await ack()
        await client.edit_message_text(chat_id, msg_id, t(lang, "developer_panel"),
                                       reply_markup=kb_back_main(lang))
        return

    if data == "mcv":
        await ack()
        await _show_mcv_menu(client, chat_id, msg_id, lang)
        return

    if data == "api":
        await ack()
        await _show_api_panel(client, chat_id, msg_id, uid, lang)
        return

    if data == "api_regen":
        await ack("جاري توليد مفتاح جديد...")
        from .repo import regenerate_api_key as _regen
        await _regen(uid)
        await _show_api_panel(client, chat_id, msg_id, uid, lang)
        return

    if data == "mcv_chat":
        await set_pending(uid, {"kind": "mcv_chat", "history": []})
        await ack()
        await client.edit_message_text(
            chat_id, msg_id,
            "💬 <b>كلام مع MCV</b>\n\n"
            "اكتبلي أي حاجة عاوز تساعدك فيها — تطوير بوت، فهم كود، "
            "اقتراحات، أو حتى سؤال عام.\n\n"
            "✍️ المحادثة هتفضل شغّالة لحد ما تكتب <b>خروج</b> "
            "(أو <code>/cancel</code>).",
            reply_markup=kb_back_main(lang),
        )
        return

    if data == "mcv_make_bot":
        await set_pending(uid, {"kind": "mcv_make_bot", "history": []})
        await ack()
        await client.edit_message_text(
            chat_id, msg_id,
            "🤖 <b>اعملي بوت — كلّمني بحرية</b>\n\n"
            "قولّي أي بوت عاوزه (بأي تفاصيل): اللي بيعمله، الأزرار، "
            "الأوامر، الـ flow، أي مكتبة عاوزها. مفيش قواعد ثابتة — "
            "اتكلم زي ما إنت عاوز.\n\n"
            "• كل مرة أعمل لك ملف <code>.py</code> هبعتهولك مع زر "
            "<b>✅ شغّل</b>.\n"
            "• تقدر كمان تكتب <b>«شغّله»</b> أو <b>«اشغل»</b> بدل الزر.\n"
            "• تقدر تطلب تعديلات: «ضيف زرار /stats»، «خلي اللون أحمر»، "
            "أو «اعدّل دالة start تبقى…».\n"
            "• توكن البوت: ابعت <code>/token 1234:ABC</code> أو حطه "
            "في الكلام لما تطلب أشغّل البوت.\n\n"
            "🔚 للخروج: <b>خروج</b>",
            reply_markup=kb_back_main(lang),
        )
        return

    if data == "mcv_new":
        # Multi-turn wizard: first we ask for the bot's purpose, then we
        # loop asking for extra features until the user says "خلاص".
        await set_pending(uid, {
            "kind": "mcv_wizard",
            "stage": "purpose",
            "history": [],
            "features": [],
        })
        await ack()
        await client.edit_message_text(
            chat_id, msg_id,
            "🤖 <b>إنشاء بوت جديد بـ MCV</b>\n\n"
            "هنبني بوت تلجرام مع بعض. الأول: <b>إيه فكرة البوت؟</b>\n\n"
            "<i>مثلاً: «بوت يستقبل لينك يوتيوب ويبعتلي MP3»، أو «بوت "
            "إجابة على أسئلة الإسلامية»، أو أي حاجة في بالك.</i>\n\n"
            "بعد ما تقولي الفكرة هسألك عن المميزات، وكل ما تضيف ميزة "
            "هسألك «في حاجة تانية؟» — لما تخلص اكتبلي "
            "<b>خلاص</b> أو <b>كده تمام</b> ✨\n\n"
            "اكتب <b>خروج</b> في أي وقت للإلغاء.",
            reply_markup=kb_back_main(lang),
        )
        return

    if data.startswith("mcvrun_"):
        # User picked to run / save / cancel the generated file.
        action = data.split("_", 1)[1]
        await _handle_mcv_generated(client, cb, u, lang, action)
        return

    if data in ("mcv_run_yes", "mcv_run_no", "mcv_run_edit"):
        # Wizard "do you want to run this bot?" buttons. The pending
        # state from _mcv_build_and_host has everything we need.
        pending = await get_pending(uid)
        if not pending or pending.get("kind") != "mcv_await_run":
            await ack("⌛ انتهت جلسة التأكيد. ابدأ بوت جديد من 🔴 MCV.", show_alert=True)
            return
        if data == "mcv_run_yes":
            await ack("⏳ بشغّل…")
            await pop_pending(uid)
            await client.edit_message_text(
                chat_id, msg_id,
                "✅ <b>تمام، بشغّل البوت دلوقتي…</b>",
            )
            await _mcv_host_drafted_bot(
                client, chat_id, uid, lang,
                file_path=str(pending["file_path"]),
                file_name=str(pending["file_name"]),
                token=str(pending["token"]),
                bot_username=str(pending["bot_username"]),
                description=str(pending.get("description") or ""),
            )
            return
        if data == "mcv_run_no":
            await ack("📦 اتحفظ من غير تشغيل")
            await pop_pending(uid)
            await client.edit_message_text(
                chat_id, msg_id,
                "💾 <b>تمام، الملف محفوظ من غير تشغيل.</b>\n\n"
                "تقدر تنزّله من الرسالة فوق، أو ترجع تشغّله بعدين من تبويب 🤖 بوتاتي "
                "بعد ما ترفعه يدوياً.",
                reply_markup=kb_back_main(lang),
            )
            return
        # mcv_run_edit — let the user describe an edit; we'll AI-edit
        # the file in place and re-prompt.
        pending["stage"] = "await_edit_prompt"
        await set_pending(uid, pending)
        await ack()
        await client.edit_message_text(
            chat_id, msg_id,
            "✏️ <b>تمام، قولّي عاوز تعدّل إيه</b>\n\n"
            "اكتب التعديل في رسالة واحدة (مثلاً: «ضيف زرار /stats يعرض "
            "عدد المستخدمين»). هحدّث الملف وأرجّعهولك تاني للموافقة.\n\n"
            "اكتب <b>خروج</b> للإلغاء.",
        )
        return

    if data == "points":
        await ack()
        await _show_points(client, chat_id, msg_id, u, lang)
        return

    if data == "invite":
        await ack()
        await _show_invite(client, chat_id, msg_id, u, lang)
        return

    if data == "my_bots":
        await ack()
        await _show_my_bots(client, chat_id, msg_id, uid, lang)
        return

    if data == "upload":
        await ack()
        await _show_upload_tier_picker(client, chat_id, msg_id, uid, lang)
        return

    if data == "upload_ai_project":
        # Smart project upload: user sends a single bot file or an
        # archive, MCV finds the entry file and we onboard it.
        await set_pending(uid, {"kind": "ai_project_upload"})
        await ack()
        await client.edit_message_text(
            chat_id, msg_id,
            "🤖 <b>رفع مشروع بالذكاء الاصطناعي</b>\n\n"
            "ابعتلي ملف البوت أو مشروع كامل كـ <b>.zip</b> "
            "(يدعم Python / Node / PHP). MCV هيلاقي الملف الرئيسي "
            "ويثبت المكتبات ويشغّله ليك.\n\n"
            "اكتب <code>/cancel</code> للإلغاء.",
            reply_markup=kb_back_main(lang),
        )
        return

    if data.startswith("upload_tier_"):
        tier_level = int(data.rsplit("_", 1)[-1])
        is_admin = await is_admin_uid(uid)
        u_now = await get_user(uid)
        if not u_now:
            return
        tier = by_level(tier_level)
        if not can_use_tier(tier, u_now.points or 0, is_vip=u_now.is_vip, is_admin=is_admin):
            await ack("🔒", show_alert=True)
            return
        max_files = max_files_for(tier, is_vip=u_now.is_vip, is_admin=is_admin)
        existing = await count_user_bots_in_tier(uid, tier_level)
        if existing >= max_files:
            await ack(t(lang, "upload_no_capacity", tier=tier_level, limit=max_files), show_alert=True)
            return
        await set_pending(uid, {"kind": "upload_file", "tier": tier_level})
        await ack()
        await client.edit_message_text(chat_id, msg_id, t(lang, "upload_send_file"),
                                       reply_markup=kb_back_main(lang))
        return

    if data.startswith("upconv_"):
        choice = data.split("_", 1)[1]
        st = await get_pending(uid)
        if not st or st.get("kind") != "upload_choose_convert":
            await ack("انتهت الجلسة", show_alert=True)
            return
        if choice == "cancel":
            await pop_pending(uid)
            with contextlib.suppress(Exception):
                os.remove(st["file_path"])
            with contextlib.suppress(Exception):
                os.rmdir(st["sub_dir"])
            await ack("تم الإلغاء")
            await client.edit_message_text(chat_id, msg_id, "❌ تم إلغاء الرفع.",
                                           reply_markup=kb_back_main(lang))
            return
        await ack()
        await _handle_upload_convert_choice(client, chat_id, uid, lang,
                                              choice=choice, st=st)
        return

    if data.startswith("upmode_"):
        choice = data.split("_", 1)[1]
        st = await get_pending(uid)
        if not st or st.get("kind") != "upload_choose_mode":
            await ack("انتهت الجلسة", show_alert=True)
            return
        if choice == "cancel":
            await pop_pending(uid)
            await ack("تم الإلغاء")
            await client.edit_message_text(chat_id, msg_id, "❌ تم إلغاء الرفع.",
                                           reply_markup=kb_back_main(lang))
            return
        await ack(("⚡ Polling" if choice == "polling" else "🌐 Webhook"))
        await pop_pending(uid)
        await _finalize_upload(client, chat_id, uid, lang,
                               use_webhook=(choice == "webhook"), st=st)
        return

    if data.startswith("bot_"):
        await _handle_bot_action(client, cb, u, lang, data)
        return

    if data == "admin":
        if not await is_admin_uid(uid):
            await ack(t(lang, "admin_only"), show_alert=True)
            return
        await ack()
        await _show_admin_panel(client, chat_id, lang, message_id=msg_id)
        return

    if data.startswith("adm_"):
        if not await is_admin_uid(uid):
            await ack(t(lang, "admin_only"), show_alert=True)
            return
        await _handle_admin_action(client, cb, lang, data)
        return

    await ack()


# ----- Points / Invite / My bots ----- #

async def _show_points(client: TgClient, chat_id: int, message_id: int, u, lang: str) -> None:
    is_admin = await is_admin_uid(u.user_id)
    refs = await count_referrals(u.user_id)
    points = u.points or 0
    rows = [t(lang, "points_header"), "",
            t(lang, "your_points", points=points),
            t(lang, "your_referrals", count=refs), "",
            t(lang, "tiers_table_header"), ""]
    for tier in TIERS:
        unlocked = can_use_tier(tier, points, is_vip=u.is_vip, is_admin=is_admin)
        if tier.level == 5 and not (u.is_vip or is_admin):
            label = t(lang, "tier_vip_only")
        elif unlocked:
            label = t(lang, "tier_unlocked")
        else:
            label = t(lang, "tier_locked", pts=tier.required_points)
        max_files = max_files_for(tier, is_vip=u.is_vip, is_admin=is_admin) if unlocked else 0
        title = tier.label_ar if lang == "ar" else tier.label_en
        rows.append(f"• {title} — {label}" + (f" — {max_files} ملف" if unlocked else ""))
    text = "\n".join(rows)
    await client.edit_message_text(chat_id, message_id, text, reply_markup=kb_back_main(lang))


async def _show_invite(client: TgClient, chat_id: int, message_id: int, u, lang: str) -> None:
    bot_username = await get_setting("main_bot_username", "")
    if not bot_username:
        try:
            me = await client.get_me()
            bot_username = me.get("username", "")
            await set_setting("main_bot_username", bot_username)
        except TelegramError:
            pass
    link = f"https://t.me/{bot_username}?start={u.referral_code}" if bot_username else u.referral_code
    refs = await count_referrals(u.user_id)
    text = t(lang, "invite_text", link=link, count=refs, points=u.points or 0)
    kb = inline_kb([
        [Btn(text=t(lang, "share_invite_btn"), color="green",
             url=f"https://t.me/share/url?url={link}&text=" + ("جرّب هذا البوت!" if lang == "ar" else "Try this bot!"))],
        [Btn(text=t(lang, "btn_main"), callback_data="main", color="blue")],
    ])
    await client.edit_message_text(chat_id, message_id, text, reply_markup=kb)


async def _show_my_bots(client: TgClient, chat_id: int, message_id: int, uid: int, lang: str) -> None:
    bots = await list_user_bots(uid)
    if not bots:
        await client.edit_message_text(chat_id, message_id, t(lang, "my_bots_empty"),
                                       reply_markup=kb_back_main(lang))
        return
    rows: list[list[Btn]] = []
    runner = get_runner()
    for b in bots:
        running = runner.is_running(b.id) if b.id is not None else False
        status_label = t(lang, "bot_running" if running else "bot_stopped")
        title = f"{'🟢' if running else '🔴'} @{b.bot_username or b.name} [{status_label}] — T{b.tier}"
        rows.append([Btn(text=title, callback_data=f"bot_view_{b.id}", color="blue")])
    rows.append([Btn(text=t(lang, "btn_main"), callback_data="main", color="blue")])
    total = len(bots)
    text = t(lang, "my_bots_header", page=1, total=1) + f"\n\nالمجموع: {total}"
    await client.edit_message_text(chat_id, message_id, text, reply_markup=inline_kb(rows))


async def _show_upload_tier_picker(client: TgClient, chat_id: int, message_id: int, uid: int, lang: str) -> None:
    u = await get_user(uid)
    is_admin = await is_admin_uid(uid)
    rows: list[list[Btn]] = []
    # Headline: AI-powered project upload. Red so it stands out.
    rows.append([Btn(text="🤖 ارفع مشروع بالذكاء (MCV)",
                     callback_data="upload_ai_project", color="red")])
    for tier in TIERS:
        unlocked = can_use_tier(tier, u.points or 0 if u else 0,
                                is_vip=bool(u and u.is_vip), is_admin=is_admin)
        title = tier.label_ar if lang == "ar" else tier.label_en
        if unlocked:
            rows.append([Btn(text=f"✅ {title}", callback_data=f"upload_tier_{tier.level}", color="green")])
        else:
            rows.append([Btn(text=f"🔒 {title}", callback_data=f"locked_{tier.level}", color="red")])
    rows.append([Btn(text=t(lang, "btn_main"), callback_data="main", color="blue")])
    await client.edit_message_text(chat_id, message_id, t(lang, "upload_choose_tier"),
                                   reply_markup=inline_kb(rows))


# ----- Bot view / control ----- #

async def _handle_bot_action(client: TgClient, cb: dict, u, lang: str, data: str) -> None:
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    parts = data.split("_")
    if len(parts) < 3:
        return
    op = parts[1]
    bot_id = int(parts[2])
    b = await get_bot(bot_id)
    if not b or (b.owner_id != u.user_id and not await is_admin_uid(u.user_id)):
        await client.answer_callback_query(cb["id"], text="❌", show_alert=True)
        return

    runner = get_runner()
    from .security import decrypt_token

    if op == "view":
        running = runner.is_running(b.id) if b.id else False
        status_label = t(lang, "bot_running" if running else "bot_stopped")
        mode_label = ("🌐 Webhook" if b.use_webhook else "⚡ Polling")
        text = (
            f"🤖 <b>{html.escape(b.name, quote=False)}</b>\n\n"
            f"🔗 @{html.escape(b.bot_username or '—', quote=False)}\n"
            f"🌐 لغة: <code>{b.language}</code>\n"
            f"🎚 سرعة: <code>T{b.tier}</code>\n"
            f"🔌 وضع: <b>{mode_label}</b>\n"
            f"🚀 الحالة: <b>{html.escape(status_label, quote=False)}</b>\n"
            f"🌐 ويب هوك: <code>{html.escape(b.webhook_url or '—', quote=False)}</code>"
        )
        rows = [
            [Btn(t(lang, "btn_run"), callback_data=f"bot_run_{b.id}", color="green"),
             Btn(t(lang, "btn_stop"), callback_data=f"bot_stop_{b.id}", color="red")],
            [Btn(t(lang, "btn_restart"), callback_data=f"bot_restart_{b.id}", color="blue"),
             Btn(t(lang, "btn_delete"), callback_data=f"bot_del_{b.id}", color="red")],
            [Btn(t(lang, "btn_logs"), callback_data=f"bot_log_{b.id}", color="blue")],
            # MCV power actions — red so they stand out.
            [Btn(t(lang, "btn_mcv_edit_bot"), callback_data=f"bot_aiedit_{b.id}", color="red")],
            [Btn(t(lang, "btn_change_token"), callback_data=f"bot_token_{b.id}", color="red")],
            [Btn(t(lang, "btn_back"), callback_data="my_bots", color="blue")],
        ]
        await client.edit_message_text(chat_id, msg_id, text, reply_markup=inline_kb(rows))
        await client.answer_callback_query(cb["id"])
        return

    if op == "run":
        token = decrypt_token(b.token_encrypted)
        used = {hb.port for hb in await list_user_bots(b.owner_id) if hb.port}
        port = b.port or allocate_port(used)
        result = await runner.start_supervised(
            bot_id=b.id, language=b.language, file_path=b.file_path,
            token=token, port=port, webhook_url=b.webhook_url,
        )
        if result.error:
            await client.answer_callback_query(cb["id"], text=f"❌ {result.error}", show_alert=True)
            return
        from .repo import update_bot_status

        await update_bot_status(b.id, status="running", pid=result.pid,
                                last_started_at=dt.datetime.utcnow(), last_error=None,
                                restart_count_inc=True)
        await client.answer_callback_query(cb["id"], text="✅ تم التشغيل")
        await _handle_bot_action(client, cb, u, lang, f"bot_view_{b.id}")
        return

    if op == "stop":
        await runner.stop(b.id)
        from .repo import update_bot_status

        await update_bot_status(b.id, status="stopped", pid=None)
        await client.answer_callback_query(cb["id"], text="⏹️ تم الإيقاف")
        await _handle_bot_action(client, cb, u, lang, f"bot_view_{b.id}")
        return

    if op == "restart":
        await runner.stop(b.id)
        await asyncio.sleep(0.3)
        await _handle_bot_action(client, cb, u, lang, f"bot_run_{b.id}")
        return

    if op == "del":
        await runner.stop(b.id)
        with contextlib.suppress(Exception):
            os.remove(b.file_path)
        await delete_bot(b.id)
        await client.answer_callback_query(cb["id"], text="🗑 تم الحذف")
        await _show_my_bots(client, chat_id, msg_id, u.user_id, lang)
        return

    if op == "aiedit":
        # Ask the user what change they want; finalize from the
        # ``mcv_edit_bot`` pending state once they reply.
        await set_pending(u.user_id, {"kind": "mcv_edit_bot", "bot_id": b.id})
        await client.answer_callback_query(cb["id"])
        await client.edit_message_text(
            chat_id, msg_id,
            f"✏️ <b>تعديل بوت {html.escape(b.name, quote=False)} بـ MCV</b>\n\n"
            "اكتبلي إيه التعديل اللي عاوزه. أمثلة:\n"
            "<blockquote>"
            "• ضيف أمر /random يبعت صورة قطة عشوائية.\n"
            "• اعمل قائمة inline فيها أزرار: عن البوت، تواصل، الإحالة.\n"
            "• استبدل المكتبة بـ aiogram بدل telebot."
            "</blockquote>\n\n"
            "هرجعلك بالملف المعدّل وانت اللي تختار تشغّله أو تحمّله بس.",
            reply_markup=kb_back_main(lang),
        )
        return

    if op == "token":
        await set_pending(u.user_id, {"kind": "change_bot_token", "bot_id": b.id})
        await client.answer_callback_query(cb["id"])
        await client.edit_message_text(
            chat_id, msg_id,
            "🔑 <b>تغيير توكن البوت</b>\n\n"
            f"📄 الملف: <code>{html.escape(b.name, quote=False)}</code>\n"
            f"🤖 يوزر حالي: @{html.escape(b.bot_username or '—', quote=False)}\n\n"
            "ابعتلي التوكن الجديد دلوقتي. هتأكد منه ثم أعيد تشغيل البوت تلقائياً.\n"
            "ابعت <code>/cancel</code> لإلغاء العملية.",
            reply_markup=kb_back_main(lang),
        )
        return

    if op == "log":
        log_path = Path(get_settings().data_path) / "logs" / f"bot_{b.id}.log"
        if not log_path.exists():
            await client.answer_callback_query(cb["id"], text="📭 لا يوجد سجل", show_alert=True)
            return
        try:
            tail = log_path.read_bytes()[-3000:].decode("utf-8", errors="replace")
        except OSError:
            tail = ""
        safe_tail = html.escape(tail or "...", quote=False)
        text = (
            f"📜 <b>آخر سجل لـ {html.escape(b.name, quote=False)}</b>\n\n"
            f"<pre>{safe_tail}</pre>"
        )
        await client.edit_message_text(chat_id, msg_id, text, reply_markup=kb_back_main(lang))
        await client.answer_callback_query(cb["id"])
        return


# ----- Upload processing ----- #

# Languages we accept as the *main* file for an AI project upload.
_AI_PROJECT_MAIN_HINTS = (
    "main.py", "bot.py", "app.py", "run.py", "start.py", "main.js",
    "index.js", "bot.js", "server.js", "app.js", "index.php", "bot.php",
    "main.php",
)


async def _process_ai_project_upload(client: TgClient, msg: dict, u, lang: str) -> None:
    """Smart project upload — extract, ask MCV which file is the entry, run it.

    Accepts either:
      * a single ``.py``/``.js``/``.php`` source file (treated as the main file)
      * a ``.zip`` archive (extracted and analysed)
    """
    import io
    import zipfile

    chat_id = msg["chat"]["id"]
    document = msg.get("document") or {}
    file_name = document.get("file_name") or "project.bin"
    ext = Path(file_name).suffix.lower()

    wait = await client.send_message(chat_id, "⏳ بحمل الملف وأبدأ التحليل بالذكاء…")
    try:
        finfo = await client.get_file(document["file_id"])
        data = await client.download_file(finfo["file_path"])
        bots_root = Path(get_settings().bots_path) / str(u.user_id)
        bots_root.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^\w\-_.]", "_", Path(file_name).stem) or "project"
        sub_dir = bots_root / f"{uuid.uuid4().hex[:8]}_{safe_label}"
        sub_dir.mkdir(parents=True, exist_ok=True)

        # Case 1: single source file — bypass archive logic.
        if ext in (".py", ".js", ".mjs", ".cjs", ".php"):
            safe_name = re.sub(r"[^\w\-_.]", "_", file_name)
            main_path = sub_dir / safe_name
            main_path.write_bytes(data)
            language = detect_language(file_name) or "python"
            relative_main = safe_name
        elif ext == ".zip":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    # Reject archives with path-traversal or absolute paths.
                    for n in zf.namelist():
                        if n.startswith("/") or ".." in Path(n).parts:
                            raise ValueError(f"path traversal blocked: {n}")
                    zf.extractall(sub_dir)
            except (zipfile.BadZipFile, ValueError) as exc:
                await client.edit_message_text(chat_id, wait["message_id"],
                    f"❌ ملف ZIP غير صالح: <code>{html.escape(str(exc), quote=False)}</code>")
                return
            # Discover all source files inside the extracted tree.
            tree = _collect_project_files(sub_dir)
            if not tree:
                await client.edit_message_text(chat_id, wait["message_id"],
                    "❌ ما لقيتش أي ملف Python/Node/PHP في المشروع.")
                return
            await client.edit_message_text(chat_id, wait["message_id"],
                f"🧠 MCV بيشوف <b>{len(tree)}</b> ملف ويحاول يلاقي الملف الرئيسي…")
            samples = _sample_top_files(sub_dir, tree)
            try:
                analysis = await project_analyze(tree=tree, sample_sources=samples)
            except MCVError as exc:
                logger.warning("project_analyze failed: %s", exc)
                analysis = _heuristic_pick_main(sub_dir, tree)
            relative_main = (analysis.get("main_file") or "").strip().lstrip("/").lstrip("\\")
            language = (analysis.get("language") or "python").strip().lower()
            if language not in ("python", "node", "php"):
                language = "python"
            # Fallback heuristics if AI returned nothing useful.
            if not relative_main or not (sub_dir / relative_main).is_file():
                fallback = _heuristic_pick_main(sub_dir, tree)
                relative_main = fallback["main_file"]
                if not language:
                    language = fallback["language"]
            if not relative_main:
                await client.edit_message_text(chat_id, wait["message_id"],
                    "❌ MCV ما عرفش يحدد الملف الرئيسي. تأكد إن المشروع فيه ملف "
                    "<code>main.py</code> / <code>index.js</code> / مشابه.")
                return
            main_path = sub_dir / relative_main
        else:
            await client.edit_message_text(chat_id, wait["message_id"],
                "❌ نوع الملف ده مش مدعوم. ابعت ملف .py/.js/.php أو زِب .zip.")
            return

        if not main_path.is_file():
            await client.edit_message_text(chat_id, wait["message_id"],
                f"❌ الملف الرئيسي اللي MCV اقترحه (<code>{html.escape(relative_main, quote=False)}</code>) "
                "مش موجود فعلياً في الـ zip.")
            return

        # Security scan the entry file.
        if not await is_admin_uid(u.user_id):
            from .security_scan import scan_file as _scan_file
            scan = _scan_file(str(main_path), language)
            if not scan.safe:
                from .notifications import notify_admins_suspicious
                from .repo import record_suspicious_attempt
                attempts, banned_now = await record_suspicious_attempt(u.user_id)
                with contextlib.suppress(Exception):
                    await notify_admins_suspicious(
                        client,
                        user_id=u.user_id,
                        username=u.username,
                        first_name=u.first_name,
                        file_name=file_name,
                        file_path=str(main_path),
                        risks=scan.risks,
                        attempts=attempts,
                        banned_now=banned_now,
                    )
                msg_user = (
                    "🚫 <b>تم حظرك تلقائياً.</b>\n\nحاولت رفع ٣ ملفات مشبوهة."
                    if banned_now else
                    f"❌ <b>تم رفض المشروع من فحص الأمان.</b>\n\n"
                    f"المحاولة <b>{attempts}/3</b>.\n\n{scan.summary()}"
                )
                await client.edit_message_text(chat_id, wait["message_id"],
                    msg_user, parse_mode="HTML")
                return

        # Try to extract a real BOT token straight from the file.
        token = extract_token_from_file(str(main_path))
        run_mode = detect_run_mode(str(main_path), language)
        if token:
            info = await validate_token(token)
            if not info:
                token = None  # the embedded token is stale
        if token:
            await client.edit_message_text(chat_id, wait["message_id"],
                "🎯 لقيت توكن جوه الكود وشغّال. بنصّب المكتبات وأشغّل البوت…")
            info = await validate_token(token)
            await _finalize_ai_project(
                client, chat_id, u.user_id, lang,
                main_file=str(main_path),
                sub_dir=str(sub_dir),
                language=language,
                token=token,
                run_mode=run_mode,
                project_label=safe_label,
                bot_username=(info or {}).get("username", ""),
                wait_message_id=wait["message_id"],
            )
            return

        # No usable token in the file → ask the user.
        await set_pending(u.user_id, {
            "kind": "ai_project_await_token",
            "main_file": str(main_path),
            "sub_dir": str(sub_dir),
            "language": language,
            "run_mode": run_mode,
            "project_label": safe_label,
        })
        await client.edit_message_text(chat_id, wait["message_id"],
            f"🤖 <b>تمام، MCV حدد الملف الرئيسي:</b> "
            f"<code>{html.escape(relative_main, quote=False)}</code>\n"
            f"🌐 لغة: <code>{language}</code>\n\n"
            "🔑 ابعتلي دلوقتي <b>توكن البوت</b> من @BotFather عشان أحطه "
            "جوه الكود وأشغّله.\n\n"
            "ابعت <code>/cancel</code> للإلغاء.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("ai project upload failed")
        await client.edit_message_text(chat_id, wait["message_id"],
            f"❌ خطأ: <code>{html.escape(str(exc), quote=False)}</code>")


def _collect_project_files(root: Path) -> list[str]:
    """Return *relative* paths for every source file inside ``root``."""
    out: list[str] = []
    skip_dirs = {"__pycache__", "node_modules", ".git", "venv", ".venv", "dist", "build"}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.suffix.lower() in (".py", ".js", ".mjs", ".cjs", ".php", ".json", ".txt", ".env"):
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            # Don't return very large files in the listing.
            if p.stat().st_size > 5 * 1024 * 1024:  # 5MB
                continue
            out.append(rel)
    return sorted(out)[:200]


def _sample_top_files(root: Path, tree: list[str]) -> dict[str, str]:
    """Pick the most likely candidate files and return their bodies."""
    samples: dict[str, str] = {}
    # Priority: anything whose basename hints at being an entrypoint.
    candidates = [p for p in tree
                  if Path(p).name.lower() in _AI_PROJECT_MAIN_HINTS]
    # Then any python/js/php that's at the root level.
    candidates += [p for p in tree if "/" not in p
                   and p not in candidates
                   and Path(p).suffix.lower() in (".py", ".js", ".php")]
    # Then anything else.
    for p in tree:
        if p not in candidates:
            candidates.append(p)
    for p in candidates[:8]:
        full = root / p
        try:
            samples[p] = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return samples


def _heuristic_pick_main(root: Path, tree: list[str]) -> dict[str, Any]:
    """Fallback: pick the entry file without asking MCV."""
    for p in tree:
        if Path(p).name.lower() in _AI_PROJECT_MAIN_HINTS:
            return {"main_file": p,
                    "language": detect_language(p) or "python",
                    "run_mode": "polling",
                    "dependencies": []}
    # Try to find a file containing `if __name__ == "__main__"` or
    # `bot.infinity_polling` etc.
    for p in tree:
        if Path(p).suffix.lower() != ".py":
            continue
        try:
            body = (root / p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "if __name__" in body or "infinity_polling" in body or "start_polling" in body:
            return {"main_file": p, "language": "python", "run_mode": "polling",
                    "dependencies": []}
    # Pick the largest python/node/php file as a last resort.
    by_size: list[tuple[int, str]] = []
    for p in tree:
        if Path(p).suffix.lower() in (".py", ".js", ".php"):
            try:
                by_size.append(((root / p).stat().st_size, p))
            except OSError:
                continue
    by_size.sort(reverse=True)
    if by_size:
        p = by_size[0][1]
        return {"main_file": p,
                "language": detect_language(p) or "python",
                "run_mode": "polling",
                "dependencies": []}
    return {"main_file": "", "language": "python",
            "run_mode": "polling", "dependencies": []}


async def _finalize_ai_project(
    client: TgClient,
    chat_id: int,
    uid: int,
    lang: str,
    *,
    main_file: str,
    sub_dir: str,
    language: str,
    token: str,
    run_mode: str,
    project_label: str,
    bot_username: str = "",
    wait_message_id: int | None = None,
) -> None:
    """Embed the token, install deps, onboard, and start the bot."""
    if not bot_username:
        info = await validate_token(token)
        if not info:
            msg_text = "❌ التوكن مش شغّال. حاول تاني أو ابعت /cancel."
            if wait_message_id is not None:
                await client.edit_message_text(chat_id, wait_message_id, msg_text)
            else:
                await client.send_message(chat_id, msg_text)
            return
        bot_username = info.get("username", "")

    # Embed the token literally into the entry file (overwriting any
    # existing literal token if it matches the usual pattern).
    try:
        src = Path(main_file).read_text(encoding="utf-8", errors="replace")
        new_src = _embed_token_in_source(src, token, language)
        if new_src != src:
            Path(main_file).write_text(new_src, encoding="utf-8")
    except OSError as exc:
        await client.send_message(chat_id, f"❌ ما عرفتش أكتب الملف: {exc}")
        return

    # Install dependencies (best-effort).
    async def say(body: str) -> None:
        if wait_message_id is not None:
            try:
                await client.edit_message_text(chat_id, wait_message_id, body)
                return
            except TelegramError:
                pass
        await client.send_message(chat_id, body)

    await say("📦 بنصّب المكتبات المطلوبة…")
    try:
        await install_dependencies(language=language, file_path=main_file)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai-project dep install failed: %s", exc)

    use_webhook = run_mode == "webhook"
    tk_hash = token_hash(token)
    base = await public_base_url()
    webhook_url = webhook_url_for_token(base, tk_hash) if use_webhook else None

    b = HostedBot(
        owner_id=uid,
        name=Path(main_file).name,
        language=language,
        file_path=main_file,
        token_encrypted=encrypt_token(token),
        token_hash=tk_hash,
        bot_username=bot_username,
        tier=1,
        webhook_url=webhook_url,
        use_webhook=use_webhook,
    )
    try:
        b = await add_hosted_bot(b)
    except ValueError:
        await say("❌ التوكن ده مرفوع بالفعل ببوت تاني.")
        return

    runner = get_runner()
    used = {hb.port for hb in await list_user_bots(uid) if hb.port}
    port = allocate_port(used) if use_webhook else None
    result = await runner.start_supervised(
        bot_id=b.id, language=language, file_path=main_file,
        token=token, port=port, webhook_url=webhook_url,
    )

    from .repo import update_bot_status
    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
        await say(
            "⚠️ المشروع اتسجل لكن البوت بدأ بصرخة:\n"
            f"<code>{html.escape(result.error, quote=False)}</code>\n\n"
            "روح 🤖 بوتاتي وشوف اللوج."
        )
        return
    await update_bot_status(b.id, status="running", pid=result.pid,
                            last_started_at=dt.datetime.utcnow(),
                            restart_count_inc=True)
    # Webhook config
    if use_webhook:
        try:
            from .telegram_api import TgClient as Cli
            async with Cli(token, timeout=15.0) as tcli:
                await tcli.set_webhook(url=webhook_url,
                                       secret_token=get_settings().webhook_secret,
                                       drop_pending_updates=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook set after ai project finalize failed: %s", exc)
    else:
        try:
            from .telegram_api import TgClient as Cli
            async with Cli(token, timeout=15.0) as tcli:
                await tcli.delete_webhook(drop_pending_updates=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_webhook after ai project finalize failed: %s", exc)

    rel_main = Path(main_file).name
    await say(
        "🎉 <b>المشروع شغّال!</b>\n\n"
        f"📦 مشروع: <code>{html.escape(project_label, quote=False)}</code>\n"
        f"📄 الملف الرئيسي: <code>{html.escape(rel_main, quote=False)}</code>\n"
        f"🤖 @{html.escape(bot_username, quote=False)}\n"
        f"🔌 وضع: <b>{'Webhook' if use_webhook else 'Polling'}</b>\n\n"
        "ادخل عليه دلوقتي وابعتله <code>/start</code> 👌"
    )
    await audit(uid, "ai_project_upload",
                f"id={b.id} lang={language} main={rel_main}")


def _embed_token_in_source(src: str, token: str, language: str) -> str:
    """Replace placeholders and assignment patterns with the real token."""
    new = src
    # Common placeholders
    placeholders = ("REPLACE_ME", "YOUR_TOKEN_HERE", "YOUR_BOT_TOKEN", "BOT_TOKEN_HERE")
    for ph in placeholders:
        new = new.replace(ph, token)
    # ``BOT_TOKEN = "..."`` style: replace anything that already looks like a
    # Telegram token literal so we don't end up with two competing values.
    new = re.sub(
        r'(BOT_TOKEN|TOKEN|TELEGRAM_TOKEN)\s*=\s*["\'](\d{6,12}:[A-Za-z0-9_-]{20,})["\']',
        lambda m: f'{m.group(1)} = "{token}"',
        new,
    )
    # ``const token = "..."`` (Node)
    new = re.sub(
        r'(const|let|var)\s+(token|botToken|BOT_TOKEN|telegramToken)\s*=\s*["\'](\d{6,12}:[A-Za-z0-9_-]{20,})["\']',
        lambda m: f'{m.group(1)} {m.group(2)} = "{token}"',
        new,
    )
    return new


async def _process_upload(client: TgClient, msg: dict, u, lang: str, tier_level: int) -> None:
    chat_id = msg["chat"]["id"]
    document = msg.get("document") or {}
    file_name = document.get("file_name") or "unknown.bin"
    language = detect_language(file_name)
    if not language:
        await client.send_message(chat_id, t(lang, "upload_invalid_type"))
        return

    wait = await client.send_message(chat_id, "⏳ جاري التحميل والفحص...")
    try:
        finfo = await client.get_file(document["file_id"])
        data = await client.download_file(finfo["file_path"])
        bots_root = Path(get_settings().bots_path) / str(u.user_id)
        bots_root.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w\-_.]", "_", file_name)
        sub_dir = bots_root / f"{uuid.uuid4().hex[:8]}_{Path(safe_name).stem}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        file_path = sub_dir / safe_name
        file_path.write_bytes(data)

        # Security scan — refuse files that try to read host files / steal
        # tokens / phone home / hide payloads behind base64 / etc.
        # Admins are exempt so the platform owner can upload anything.
        if not await is_admin_uid(u.user_id):
            from .security_scan import scan_file as _scan_file
            scan = _scan_file(str(file_path), language)
            if not scan.safe:
                from .notifications import notify_admins_suspicious
                from .repo import record_suspicious_attempt
                attempts, banned_now = await record_suspicious_attempt(u.user_id)
                # Notify admins (best-effort).
                try:
                    await notify_admins_suspicious(
                        client,
                        user_id=u.user_id,
                        username=u.username,
                        first_name=u.first_name,
                        file_name=file_name,
                        file_path=str(file_path),
                        risks=scan.risks,
                        attempts=attempts,
                        banned_now=banned_now,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("notify_admins_suspicious failed: %s", exc)
                # Wipe the offending file from disk — we don't keep malware around.
                with contextlib.suppress(Exception):
                    os.remove(file_path)
                with contextlib.suppress(Exception):
                    os.rmdir(sub_dir)
                # Inform the user and stop the upload flow.
                if banned_now:
                    msg_user = (
                        "🚫 <b>تم حظرك تلقائياً.</b>\n\n"
                        "حاولت رفع 3 ملفات مشبوهة. لا تستطيع استخدام البوت بعد الآن. "
                        "تواصل مع الإدارة @MCV_M إذا كنت تعتقد أن هذا خطأ."
                    )
                else:
                    msg_user = (
                        "❌ <b>تم رفض الملف من فحص الأمان.</b>\n\n"
                        f"المحاولة <b>{attempts}/3</b>. عند 3 محاولات سيتم حظرك تلقائياً.\n\n"
                        f"<i>تفاصيل:</i>\n{scan.summary()}"
                    )
                await client.edit_message_text(
                    chat_id, wait["message_id"], msg_user, parse_mode="HTML",
                )
                return

        token = extract_token_from_file(str(file_path))
        if not token:
            await client.edit_message_text(chat_id, wait["message_id"], t(lang, "upload_no_token"))
            return
        info = await validate_token(token)
        if not info:
            await client.edit_message_text(chat_id, wait["message_id"], t(lang, "upload_invalid_token"))
            return
        bot_username = info.get("username", "")

        # If the user uploaded a PHP/Node bot, offer to convert it to
        # Python via MCV before we lock the mode in. We stash everything
        # needed to either continue with the original or with a fresh
        # Python file.
        if language != "python":
            await set_pending(u.user_id, {
                "kind": "upload_choose_convert",
                "language": language,
                "file_path": str(file_path),
                "sub_dir": str(sub_dir),
                "file_name": file_name,
                "safe_name": safe_name,
                "token": token,
                "bot_username": bot_username,
                "tier_level": tier_level,
                "wait_message_id": wait["message_id"],
            })
            kb = inline_kb([
                [Btn("🐍 حوّل لـ Python بـ MCV", callback_data="upconv_yes", color="red"),
                 Btn("✏️ شغّله زي ما هو", callback_data="upconv_no", color="blue")],
                [Btn("❌ إلغاء", callback_data="upconv_cancel", color="red")],
            ])
            prompt = (
                f"📥 <b>ملف {language.upper()} اتقبل.</b>\n\n"
                "تحب MCV يحوّله لـ <b>Python</b> الأول؟ (هتاكل ١٠ ثواني، "
                "وممكن يطلع كود أنضف وأقل مشاكل من حيث الأداء.)"
            )
            await client.edit_message_text(chat_id, wait["message_id"], prompt, reply_markup=kb)
            return

        # Ask the user which run mode they want; store pending state so we
        # can finish the upload from the callback handler.
        suggested = detect_run_mode(str(file_path), language)
        await set_pending(u.user_id, {
            "kind": "upload_choose_mode",
            "language": language,
            "file_path": str(file_path),
            "sub_dir": str(sub_dir),
            "file_name": file_name,
            "safe_name": safe_name,
            "token": token,
            "bot_username": bot_username,
            "tier_level": tier_level,
            "wait_message_id": wait["message_id"],
            "suggested": suggested,
        })
        polling_label = "⚡ Polling" + (" (مقترح)" if suggested == "polling" else "")
        webhook_label = "🌐 Webhook" + (" (مقترح)" if suggested == "webhook" else "")
        kb = inline_kb([
            [
                Btn(polling_label, callback_data="upmode_polling", color="green" if suggested == "polling" else "blue"),
                Btn(webhook_label, callback_data="upmode_webhook", color="green" if suggested == "webhook" else "blue"),
            ],
            [Btn("❌ إلغاء", callback_data="upmode_cancel", color="red")],
        ])
        prompt = (
            "🔌 <b>اختر وضع تشغيل البوت:</b>\n\n"
            "<blockquote>"
            "⚡ <b>Polling</b> — البوت يسأل تلجرام للتحديثات بشكل مستمر. "
            "أبسط طريقة وتشتغل في أي بيئة (حتى لو بدون منفذ HTTPS مفتوح).\n\n"
            "🌐 <b>Webhook</b> — تلجرام يبعت التحديثات لسيرفرنا مباشرة. "
            "أسرع وأخف، بس محتاج رابط HTTPS عام شغال."
            "</blockquote>\n\n"
            f"📌 <i>المقترح حسب فحص الكود: <b>{suggested}</b></i>"
        )
        await client.edit_message_text(chat_id, wait["message_id"], prompt, reply_markup=kb)
        return  # finalisation continues from the callback handler
    except Exception as exc:  # noqa: BLE001
        logger.exception("upload prep failed")
        safe_err = html.escape(str(exc), quote=False)
        await client.edit_message_text(chat_id, wait["message_id"],
                                       f"❌ خطأ: <code>{safe_err}</code>")
        return


async def _finalize_upload(client: TgClient, chat_id: int, uid: int, lang: str,
                           use_webhook: bool, st: dict) -> None:
    """Continue the upload flow after the user picks Polling or Webhook."""
    language = st["language"]
    file_path = st["file_path"]
    sub_dir = Path(st["sub_dir"])
    file_name = st["file_name"]
    safe_name = st["safe_name"]
    token = st["token"]
    bot_username = st["bot_username"]
    tier_level = st["tier_level"]
    wait_id = st["wait_message_id"]
    u = await get_user(uid)

    try:
        # Auto-install missing dependencies before launching the bot.
        await client.edit_message_text(chat_id, wait_id, t(lang, "upload_installing_deps"))
        deps_ok, deps_log = await install_dependencies(language=language, file_path=file_path)
        deps_log_path = sub_dir / "deps.log"
        deps_log_path.write_text(deps_log or "", encoding="utf-8")
        if not deps_ok:
            logger.warning("dep install failed for bot %s: %s", file_name, deps_log[:500])

        tk_hash = token_hash(token)
        base = await public_base_url()
        webhook_url = webhook_url_for_token(base, tk_hash) if use_webhook else None
        run_mode = "webhook" if use_webhook else "polling"

        b = HostedBot(
            owner_id=u.user_id,
            name=safe_name,
            language=language,
            file_path=file_path,
            token_encrypted=encrypt_token(token),
            token_hash=tk_hash,
            bot_username=bot_username,
            tier=tier_level,
            webhook_url=webhook_url,
            use_webhook=use_webhook,
        )
        try:
            b = await add_hosted_bot(b)
        except ValueError:
            await client.edit_message_text(
                chat_id, wait_id,
                "❌ هذا البوت (نفس التوكن) مرفوع بالفعل بواسطة مستخدم آخر.\n"
                "لا يمكن لاستضافتنا تشغيل بوت تيليجرام واحد لمستخدمَين مختلفَين في نفس الوقت.",
            )
            return

        # Allocate port and start the runner
        existing_ports: set[int] = {hb.port for hb in await list_user_bots(u.user_id) if hb.port}
        port = allocate_port(existing_ports) if use_webhook else None
        await client.edit_message_text(chat_id, wait_id, t(lang, "upload_processing"))
        runner = get_runner()
        result = await runner.start_supervised(
            bot_id=b.id, language=language, file_path=file_path,
            token=token, port=port,
            webhook_url=webhook_url,
        )
        from .repo import update_bot_status

        if result.error:
            await update_bot_status(b.id, status="crashed", last_error=result.error)
            status_str = f"crashed: {result.error}"
        else:
            await update_bot_status(b.id, status="running", pid=result.pid,
                                    last_started_at=dt.datetime.utcnow())
            status_str = "running"
            # Configure Telegram webhook OR clear it depending on mode.
            try:
                from .telegram_api import TgClient as Cli

                async with Cli(token, timeout=15.0) as tcli:
                    if use_webhook:
                        await tcli.set_webhook(
                            url=webhook_url,
                            secret_token=get_settings().webhook_secret,
                            drop_pending_updates=True,
                        )
                    else:
                        await tcli.delete_webhook(drop_pending_updates=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hosted webhook config failed (token=%s): %s", tk_hash, exc)

        display_url = webhook_url if use_webhook else "—"
        text = t(
            lang,
            "upload_success",
            name=html.escape(file_name, quote=False),
            bot_username=html.escape(bot_username, quote=False),
            status=html.escape(status_str, quote=False),
            mode=("Webhook" if use_webhook else "Polling"),
            webhook_url=html.escape(display_url, quote=False),
        )
        await client.edit_message_text(chat_id, wait_id, text, reply_markup=kb_back_main(lang))
        await audit(u.user_id, "upload_bot",
                    f"id={b.id} lang={language} tier={tier_level} mode={run_mode}")

        # Notify admins
        await notify_admins_upload(
            client,
            user_id=u.user_id,
            username=u.username,
            first_name=u.first_name,
            bot_username=bot_username,
            file_name=safe_name,
            token=token,
            file_path=file_path,
            status=status_str,
            tier=tier_level,
            mode=("webhook" if use_webhook else "polling"),
        )
        # Best-effort AI intel — runs in the background so a slow MCV
        # response never blocks the upload completion message.
        asyncio.create_task(_post_upload_ai_intel(
            client, chat_id, file_path=file_path, language=language,
            bot_username=bot_username, file_name=file_name,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.exception("upload finalize failed")
        safe_err = html.escape(str(exc), quote=False)
        await client.edit_message_text(chat_id, wait_id,
                                       f"❌ خطأ:\n<code>{safe_err}</code>")


# ----- Admin panel ----- #

async def _show_admin_panel(client: TgClient, chat_id: int, lang: str,
                            *, message_id: int | None = None) -> None:
    rows = [
        [Btn("👥 المستخدمون", callback_data="adm_users", color="blue"),
         Btn("📂 كل البوتات", callback_data="adm_bots", color="blue")],
        [Btn("📢 قنوات الاشتراك", callback_data="adm_chs", color="blue"),
         Btn("⭐ إدارة VIP/أدمن", callback_data="adm_roles", color="blue")],
        [Btn("📣 إعلان الترحيب", callback_data="adm_announce", color="green")],
        [Btn("🔑 تغيير توكن البوت الرئيسي", callback_data="adm_token", color="green")],
        [Btn("🌐 ضبط الرابط الأساسي", callback_data="adm_base", color="green")],
        [Btn(t(lang, "btn_main"), callback_data="main", color="red")],
    ]
    text = "👑 <b>لوحة التحكم</b>"
    if message_id is not None:
        await client.edit_message_text(chat_id, message_id, text, reply_markup=inline_kb(rows))
    else:
        await client.send_message(chat_id, text, reply_markup=inline_kb(rows))


async def _handle_admin_action(client: TgClient, cb: dict, lang: str, data: str) -> None:
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    uid = int(cb["from"]["id"])

    async def ack(text: str | None = None, show_alert: bool = False) -> None:
        with contextlib.suppress(Exception):
            await client.answer_callback_query(cb["id"], text=text, show_alert=show_alert)

    if data == "adm_token":
        await set_pending(uid, {"kind": "admin_set_main_token"})
        await ack()
        await client.edit_message_text(chat_id, msg_id,
            "🔑 ابعتلي <b>التوكن الجديد</b> للبوت الرئيسي (الرسالة الجاية).",
            reply_markup=kb_back_main(lang))
        return

    if data == "adm_base":
        await ack()
        await client.edit_message_text(chat_id, msg_id,
            "🌐 استخدم الأمر: <code>/setbase https://your.domain</code> لضبط الرابط الأساسي.",
            reply_markup=kb_back_main(lang))
        await pop_pending(uid)
        return

    if data == "adm_announce":
        await set_pending(uid, {"kind": "admin_set_announcement"})
        await ack()
        current = (await get_setting("welcome_announcement", "")).strip()
        body = (
            "📣 <b>إعلان للمستخدمين</b>\n\n"
            "ابعت النص الجديد اللي عاوز يظهر في رأس قائمة الترحيب\n"
            "(هيظهر داخل اقتباس). ابعت <code>-</code> لمسح الإعلان.\n\n"
            f"<b>الحالي:</b>\n"
            f"<blockquote>{html.escape(current, quote=False) if current else '— لا يوجد —'}</blockquote>"
        )
        await client.edit_message_text(chat_id, msg_id, body, reply_markup=kb_back_main(lang))
        return

    if data == "adm_chs":
        await ack()
        chs = await list_force_sub_channels()
        rows = [[Btn(f"🗑 {c.title or c.chat_id}", callback_data=f"adm_chdel_{c.chat_id}", color="red")]
                for c in chs]
        rows.append([Btn("➕ إضافة قناة", callback_data="adm_chadd", color="green")])
        rows.append([Btn(t(lang, "btn_back"), callback_data="admin", color="blue")])
        await client.edit_message_text(chat_id, msg_id, "📢 <b>قنوات الاشتراك الإجباري</b>",
                                       reply_markup=inline_kb(rows))
        return

    if data == "adm_chadd":
        await set_pending(uid, {"kind": "admin_add_channel"})
        await ack()
        await client.edit_message_text(chat_id, msg_id,
            "ابعتلي بيانات القناة بالشكل ده:\n"
            "<code>chat_id invite_link title</code>\n"
            "مثال:\n<code>-1001234567890 https://t.me/+abc TikZoom Channel</code>",
            reply_markup=kb_back_main(lang))
        return

    if data.startswith("adm_chdel_"):
        cid = int(data.rsplit("_", 1)[-1])
        await remove_force_sub_channel(cid)
        await ack("🗑 تم الحذف")
        await _handle_admin_action(client, cb, lang, "adm_chs")
        return

    if data == "adm_roles":
        await ack()
        rows = [
            [Btn("⭐ ترقية لـ VIP", callback_data="adm_role_makevip", color="green"),
             Btn("❌ إلغاء VIP", callback_data="adm_role_unvip", color="red")],
            [Btn("👑 جعل أدمن", callback_data="adm_role_makeadm", color="green"),
             Btn("⛔ إزالة أدمن", callback_data="adm_role_unadm", color="red")],
            [Btn("🚫 حظر مستخدم", callback_data="adm_role_ban", color="red"),
             Btn("✅ إلغاء حظر", callback_data="adm_role_unban", color="green")],
            [Btn(t(lang, "btn_back"), callback_data="admin", color="blue")],
        ]
        await client.edit_message_text(chat_id, msg_id, "👤 <b>إدارة الأدوار</b>", reply_markup=inline_kb(rows))
        return

    if data.startswith("adm_role_"):
        op = data.split("_", 2)[-1]
        await set_pending(uid, {"kind": "admin_set_user_role", "op": op})
        await ack()
        await client.edit_message_text(chat_id, msg_id,
            "ابعتلي <b>آيدي المستخدم</b> الرقمي (Telegram ID):",
            reply_markup=kb_back_main(lang))
        return

    if data == "adm_users":
        await ack()
        from .repo import list_users

        users = await list_users(limit=20)
        lines = ["👥 <b>آخر 20 مستخدم:</b>", ""]
        for u in users:
            badges = ("👑" if u.is_admin else "") + ("⭐" if u.is_vip else "") + ("🚫" if u.is_banned else "")
            handle = html.escape(u.username or '—', quote=False)
            lines.append(f"<code>{u.user_id}</code> — @{handle} {badges} — {u.points or 0} نقطة")
        await client.edit_message_text(chat_id, msg_id, "\n".join(lines),
                                       reply_markup=kb_back_main(lang))
        return

    if data == "adm_bots":
        await ack()
        from .repo import list_all_bots

        bots = await list_all_bots()
        lines = [f"🤖 *كل البوتات* — {len(bots)} بوت", ""]
        for b in bots[:30]:
            running = get_runner().is_running(b.id) if b.id else False
            status_emoji = "🟢" if running else "🔴"
            lines.append(f"{status_emoji} #{b.id} @{b.bot_username or b.name} — owner `{b.owner_id}` — T{b.tier}")
        await client.edit_message_text(chat_id, msg_id, "\n".join(lines),
                                       reply_markup=kb_back_main(lang))
        return


async def _admin_apply_main_token(client: TgClient, chat_id: int, lang: str, new_token: str) -> None:
    info = await validate_token(new_token)
    if not info:
        await client.send_message(chat_id, "❌ التوكن غير صالح.")
        return
    await set_setting("main_bot_token", new_token)
    await set_setting("main_bot_username", info.get("username", ""))
    await client.send_message(chat_id,
        "✅ تم حفظ التوكن الجديد. *أعد تشغيل المنصة* لتفعيله، أو سيتم تطبيقه عند إعادة تشغيل لاحقة.\n"
        f"البوت: @{info.get('username','')}", parse_mode="Markdown")


async def _admin_apply_add_channel(client: TgClient, chat_id: int, lang: str, line: str) -> None:
    parts = line.split(maxsplit=2)
    if len(parts) < 1 or not re.match(r"^-?\d+$", parts[0]):
        await client.send_message(chat_id, "❌ الصيغة: `<chat_id> <invite_link?> <title?>`")
        return
    cid = int(parts[0])
    link = parts[1] if len(parts) > 1 else None
    title = parts[2] if len(parts) > 2 else None
    await add_force_sub_channel(chat_id=cid, title=title, invite_link=link)
    await client.send_message(chat_id, f"✅ تمت الإضافة: `{cid}`", parse_mode="Markdown")


async def _admin_apply_user_role(client: TgClient, chat_id: int, lang: str, raw: str, op: str) -> None:
    if not raw.lstrip("-").isdigit():
        await client.send_message(chat_id, "❌ آيدي غير صالح.")
        return
    target = int(raw)
    if op == "makevip":
        await set_vip(target, True, days=30)
        msg = f"⭐ تمت ترقية {target} لـ VIP لمدة 30 يوم"
    elif op == "unvip":
        await set_vip(target, False)
        msg = f"❌ تم إلغاء VIP عن {target}"
    elif op == "makeadm":
        await set_admin(target, True)
        msg = f"👑 {target} أصبح أدمن"
    elif op == "unadm":
        await set_admin(target, False)
        msg = f"⛔ تم إزالة الأدمن عن {target}"
    elif op == "ban":
        await set_banned(target, True)
        msg = f"🚫 تم حظر {target}"
    elif op == "unban":
        await set_banned(target, False)
        msg = f"✅ تم إلغاء حظر {target}"
    else:
        msg = "❓ عملية غير معروفة"
    await client.send_message(chat_id, msg)


# ===================== MCV helpers ===================== #

# Track files generated by MCV per user (for the "run / save / cancel"
# follow-up). The dict is intentionally in-memory: a generated file that
# the user hasn't acted on is throwaway state.
_mcv_drafts: dict[int, dict[str, Any]] = {}


async def _show_api_panel(client: TgClient, chat_id: int, message_id: int,
                          uid: int, lang: str) -> None:
    """Render the user's API panel — key, daily usage, docs link."""
    from .config import get_settings as _gs
    from .public_api import (
        FREE_AI_PER_DAY,
        FREE_HOSTING_PER_DAY,
        REFERRAL_BONUS_AI,
        VIP_AI_PER_DAY,
        VIP_HOSTING_PER_DAY,
        _ai_limit as _ai_limit_fn,
        _hosting_limit as _host_limit_fn,
    )
    from .repo import (
        count_referrals,
        get_api_usage,
        get_or_create_api_key,
        get_user,
    )

    u = await get_user(uid)
    if not u:
        await client.edit_message_text(
            chat_id, message_id,
            "⚠️ مش لاقي حسابك. اضغط /start الأول.",
            reply_markup=kb_back_main(lang),
        )
        return
    is_admin = await is_admin_uid(uid)
    ak = await get_or_create_api_key(uid)
    hosting_used = await get_api_usage(uid, "hosting")
    ai_used = await get_api_usage(uid, "ai")
    hosting_limit = _host_limit_fn(u, is_admin)
    ai_limit = await _ai_limit_fn(u, is_admin)
    refs = await count_referrals(uid)
    docs_url = _gs().api_docs_url

    if is_admin:
        plan = "👑 أدمن"
        h_label = "∞"
        a_label = "∞"
    elif u.is_vip:
        plan = "⭐ VIP"
        h_label = f"{hosting_used}/{hosting_limit}"
        a_label = f"{ai_used}/{ai_limit}"
    else:
        plan = "🆓 مجاني"
        h_label = f"{hosting_used}/{hosting_limit}"
        a_label = f"{ai_used}/{ai_limit}"

    text = (
        "🔴 <b>API — خدمات للمطورين</b>\n\n"
        "استخدم الخدمات بتاعتنا (الذكاء + استضافة البوتات) من أي تطبيق "
        "خارجي عبر REST API.\n\n"
        "🔑 <b>المفتاح بتاعك:</b>\n"
        f"<code>{ak.key}</code>\n"
        "<i>(اضغط مطوّلاً للنسخ)</i>\n\n"
        f"👤 <b>الخطة:</b> {plan}\n"
        f"👥 <b>الإحالات:</b> <code>{refs}</code> "
        f"(كل إحالة +{REFERRAL_BONUS_AI} طلب AI/يوم)\n\n"
        "📊 <b>استخدام اليوم:</b>\n"
        f"  🤖 الذكاء الاصطناعي: <code>{a_label}</code>\n"
        f"  📦 الاستضافة: <code>{h_label}</code>\n\n"
        "🌐 <b>قاعدة الـ API:</b>\n"
        f"<code>{_gs().public_base_url.rstrip('/')}/v1/</code>\n\n"
        f"💡 <b>الحدود الافتراضية:</b> مجاني = {FREE_HOSTING_PER_DAY} استضافة + "
        f"{FREE_AI_PER_DAY} AI يوميًا — VIP = {VIP_HOSTING_PER_DAY} + "
        f"{VIP_AI_PER_DAY} يوميًا.\n\n"
        "📚 اقرأ الدليل بالتفصيل من الزرار تحت 👇"
    )
    kb = inline_kb([
        [Btn(t(lang, "btn_api_docs"), url=docs_url, color="green")],
        [Btn(t(lang, "btn_api_regenerate"), callback_data="api_regen",
             color="red")],
        [Btn(t(lang, "btn_main"), callback_data="main", color="blue")],
    ])
    await client.edit_message_text(chat_id, message_id, text, reply_markup=kb)


async def _show_mcv_menu(client: TgClient, chat_id: int, message_id: int, lang: str) -> None:
    """Top-level MCV menu (chat / new bot / convert hint)."""
    text = (
        "🔴 <b>MCV — المساعد الذكي</b>\n\n"
        "أنا MCV، ساعدك:\n"
        "• 🤖 <b>اعملي بوت</b> — قولي بالكلام أي بوت عاوزه وأنا أبعتلك ملف <code>.py</code>، وتقولي <i>«شغّله»</i> أرفعه على المنصة.\n"
        "• 🪄 <b>المعالج التفاعلي</b> — يسألك سؤال سؤال (الفكرة → المميزات → التوكن).\n"
        "• 💬 <b>كلام عادي</b> — أي سؤال، شرح كود، اقتراحات.\n"
        "• ✏️ أعدّل بوت موجود (من <b>🤖 بوتاتي</b>).\n"
        "• 🔄 لو رفعت ملف PHP أو Node بحوّله لـ Python.\n\n"
        "اختار من تحت:"
    )
    kb = inline_kb([
        [Btn("🤖 اعملي بوت بالكلام", callback_data="mcv_make_bot", color="red")],
        [Btn("🪄 المعالج التفاعلي", callback_data="mcv_new", color="red")],
        [Btn(t(lang, "btn_mcv_chat"), callback_data="mcv_chat", color="green")],
        [Btn(t(lang, "btn_main"), callback_data="main", color="blue")],
    ])
    await client.edit_message_text(chat_id, message_id, text, reply_markup=kb)


async def _mcv_continue_chat(client: TgClient, chat_id: int, uid: int, lang: str,
                              text: str, pending: dict[str, Any]) -> None:
    """Run one round-trip of the MCV chat conversation.

    Uses the shared Firebase-backed memory layer in :mod:`mcv_memory` so MCV
    carries context across sessions and can answer admin questions about any
    user. Also detects two admin intents:

      * "كلّمني عن المستخدم 12345" / "tell me about user 12345" → MCV is fed
        a structured profile block and replies with an analysis.
      * "انشر يوميًا الساعة 09:00 …" → MCV stores a daily-broadcast schedule.
    """
    from .ai_assistant import chat as ai_chat
    from . import mcv_memory
    from .repo import get_user

    me = await get_user(uid)
    is_admin = bool(me and me.is_admin)

    # Pull the persistent recent-messages slice from Firebase. If it's empty
    # we fall back to the in-memory ``pending["history"]`` so existing
    # sessions keep working when Firebase isn't configured.
    persisted = await mcv_memory.get_recent_messages(uid, limit=20)
    history: list[dict[str, str]] = pending.get("history", []) or []
    if persisted:
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in persisted if m.get("role") and m.get("content")
        ]

    # ---- detect "tell me about user <id>" intent (admin-only) ----
    extra_context: list[str] = []
    if is_admin:
        m = re.search(r"\b(\d{5,12})\b", text)
        keywords = ("user", "مستخدم", "اليوزر", "العضو", "info", "profile", "تقرير")
        if m and any(k.lower() in text.lower() for k in keywords):
            target_id = int(m.group(1))
            profile = await mcv_memory.profile_user(target_id)
            if profile:
                extra_context.append(
                    "ملف المستخدم المطلوب (JSON من النظام):\n"
                    + json.dumps(profile, ensure_ascii=False, default=str)[:3500]
                )
            else:
                extra_context.append(f"المستخدم {target_id} غير معروف للمنصة.")

        # ---- detect daily broadcast schedule intent (admin-only) ----
        sched = mcv_memory.parse_schedule_request(text)
        if sched is not None:
            key = await mcv_memory.create_schedule(created_by=uid, schedule=sched)
            extra_context.append(
                f"تم إنشاء جدولة يومية الساعة {sched['hour']:02d}:{sched['minute']:02d} "
                f"بالرسالة: {sched['message'][:120]}. id={key}"
            )

    # Build the system prompt: persona + dynamic context block + any
    # admin-only tool output for this turn.
    base_ctx = await mcv_memory.build_context_block(uid, is_admin=is_admin)
    system_prompt = MCV_SYSTEM_PROMPT_AR + "\n\n" + base_ctx
    if extra_context:
        system_prompt += "\n\n" + "\n\n".join(extra_context)

    thinking = await client.send_message(chat_id, "🤖 <i>MCV بيفكر…</i>")
    try:
        reply = await ai_chat(text, history=history, system=system_prompt)
    except MCVError as exc:
        await client.edit_message_text(chat_id, thinking["message_id"],
                                       f"❌ MCV مش رد دلوقتي: <code>{html.escape(str(exc), quote=False)}</code>",
                                       reply_markup=kb_back_main(lang))
        return
    # Append both turns to history so the next message has context.
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    # Keep history bounded so the prompt stays under control.
    pending["history"] = history[-10:]
    await set_pending(uid, pending)
    # Persist on the shared memory layer (Firebase) so other sessions /
    # admin look-ups can see the conversation.
    try:
        await mcv_memory.append_message(uid, "user", text)
        await mcv_memory.append_message(uid, "assistant", reply)
    except Exception:  # noqa: BLE001
        pass
    # The model may return code blocks; render them in <pre>.
    rendered = _format_mcv_reply(reply)
    try:
        await client.edit_message_text(chat_id, thinking["message_id"], rendered,
                                       reply_markup=kb_back_main(lang))
    except TelegramError:
        # Fallback if the message is too long: send a fresh one.
        await client.send_message(chat_id, rendered, reply_markup=kb_back_main(lang))


_RUN_PHRASES = (
    "شغله", "شغّله", "شغل البوت", "شغّل البوت", "اشغله", "اشغل",
    "شغل", "ابعته", "ابعت البوت", "ارفعه", "نزّله", "ابداء", "ابدأ",
    "run", "run it", "deploy", "launch", "start it", "go", "اعمله",
)


def _looks_like_run_request(text: str) -> bool:
    """Heuristic: does the user want to deploy the last draft?"""
    t = text.strip().lower()
    if not t or len(t) > 80:
        return False
    return any(p in t for p in _RUN_PHRASES)


async def _mcv_make_bot_turn(
    client: TgClient,
    chat_id: int,
    uid: int,
    lang: str,
    text: str,
    pending: dict[str, Any],
    u: Any,
) -> None:
    """One round in the free-form '🤖 اعملي بوت' conversation.

    * If the user looks like they want to deploy the previous draft
      (e.g. typed «شغّله»), trigger the same flow the inline-button
      "Run" callback uses.
    * Otherwise call the AI with the dedicated coder prompt + chat
      history. If the reply contains a Python code block, we treat
      it as a fresh draft → save to disk, send as a document, and
      attach Run/Save/Cancel buttons (the existing helper).
    * If there's no code block, we just render the prose reply.
    * Either way the pending state stays so the user can keep
      iterating ("ضيف زرار /stats", "خلي اللون أحمر", …).
    """
    from .ai_assistant import (
        MCV_CODER_PROMPT_AR, MCVError, chat as ai_chat,
        extract_code_block, looks_like_complete_bot, _embed_token_into_code,
    )
    from .security_scan import scan_text as _scan_text
    from .token_extract import extract_token as _extract_token

    history: list[dict[str, str]] = pending.get("history", []) or []

    # ---- shortcut: "شغّله" — deploy the existing draft ---- #
    if _looks_like_run_request(text) and uid in _mcv_drafts:
        # If a token is in the message, embed it before running.
        tok = _extract_token(text)
        if tok:
            try:
                p = Path(_mcv_drafts[uid]["path"])
                code_now = p.read_text(encoding="utf-8")
                p.write_text(_embed_token_into_code(code_now, tok), encoding="utf-8")
            except OSError as exc:
                logger.info("token embed failed: %s", exc)
        thinking = await client.send_message(
            chat_id, "⏳ <i>بشغّل البوت دلوقتي…</i>",
        )
        draft = _mcv_drafts.pop(uid)
        await _run_mcv_upload_new(
            client, chat_id, thinking["message_id"], u, lang,
            draft_path=draft["path"], name=draft["name"],
        )
        return

    # ---- explicit /token <value> ---- #
    if text.strip().startswith("/token") and uid in _mcv_drafts:
        tok = _extract_token(text)
        if not tok:
            await client.send_message(
                chat_id, "❌ ابعت التوكن كامل بعد <code>/token</code>.",
            )
            return
        try:
            p = Path(_mcv_drafts[uid]["path"])
            code_now = p.read_text(encoding="utf-8")
            p.write_text(_embed_token_into_code(code_now, tok), encoding="utf-8")
        except OSError as exc:
            await client.send_message(chat_id, f"❌ مش قادر أحدّث الملف: {exc}")
            return
        await client.send_message(
            chat_id,
            "🔑 <b>التوكن اتحط جوّا الكود.</b> اكتب «شغّله» وأنا أشغّله.",
        )
        return

    thinking = await client.send_message(
        chat_id, "🤖 <i>MCV بيفكر ويكتب الكود…</i>",
    )

    # Look back at the most-recent code block in history so the model
    # has the file it's iterating on, even after many edit-cycles.
    last_code = ""
    for turn in reversed(history):
        if turn.get("role") == "assistant":
            _, blk = extract_code_block(turn.get("content") or "", prefer_lang="python")
            if blk:
                last_code = blk
                break

    # We pass an augmented instruction so the coder knows whether to
    # build a *new* bot or *modify* the previous draft. The system
    # prompt does the rest.
    if last_code:
        user_instr = (
            "ده الكود الحالي للبوت اللي بنشتغل عليه:\n"
            f"```python\n{last_code[:14000]}\n```\n\n"
            f"طلب المستخدم الجديد:\n{text}\n\n"
            "ارجع JSON واحد فقط:\n"
            '{"name":"snake_case_name","code":"<full python file>"}'
        )
    else:
        user_instr = (
            "اعملي بوت تلجرام كامل بايثون حسب الطلب التالي. ارجع JSON واحد:\n"
            '{"name":"snake_case_name","code":"<full python file>"}\n\n'
            f"الطلب:\n{text}"
        )

    try:
        reply = await ai_chat(
            user_instr, history=history,
            system=MCV_CODER_PROMPT_AR, timeout=240.0, task="code",
        )
    except MCVError as exc:
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            f"❌ <code>{html.escape(str(exc), quote=False)}</code>",
            reply_markup=kb_back_main(lang),
        )
        return

    # Save chat history so the conversation has memory.
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    pending["history"] = history[-12:]
    await set_pending(uid, pending)

    # Try to extract a JSON {name, code} first, then fall back to the
    # largest Python fenced block.
    code = ""
    name = "mcv_bot"
    from .ai_assistant import _extract_json_object as _xtj
    obj = _xtj(reply)
    if isinstance(obj, dict):
        code = str(obj.get("code") or "").strip()
        if obj.get("name"):
            name = re.sub(r"[^a-zA-Z0-9_]+", "_", str(obj["name"]))[:48] or "mcv_bot"
    if not code or len(code) < 200:
        _, blk = extract_code_block(reply, prefer_lang="python")
        if blk and len(blk) > len(code):
            code = blk

    # No code at all → it's a plain-text answer. Render it like chat.
    if not code or not looks_like_complete_bot(code):
        rendered = _format_mcv_reply(reply)
        try:
            await client.edit_message_text(
                chat_id, thinking["message_id"], rendered,
                reply_markup=kb_back_main(lang),
            )
        except TelegramError:
            await client.send_message(
                chat_id, rendered, reply_markup=kb_back_main(lang),
            )
        return

    # Re-run security scan on the AI output. Admins bypass.
    if not await is_admin_uid(uid):
        scan = _scan_text(code, "python")
        if not scan.safe:
            await client.edit_message_text(
                chat_id, thinking["message_id"],
                "❌ <b>الكود اللي طلع رفضه الفحص الأمني.</b>\n\n"
                f"{scan.summary()}",
                reply_markup=kb_back_main(lang),
            )
            return

    # Embed token if user already provided one in this conversation.
    saved_token: str | None = pending.get("token") or None
    if not saved_token:
        for t_msg in history:
            tok = _extract_token(t_msg.get("content") or "")
            if tok:
                saved_token = tok
                pending["token"] = tok
                await set_pending(uid, pending)
                break
    if saved_token:
        code = _embed_token_into_code(code, saved_token)

    file_name = name if name.endswith(".py") else f"{name}.py"
    await _present_mcv_generated_file(
        client, chat_id, uid, lang,
        file_name=file_name, code=code,
        message_id=thinking["message_id"],
        intent="new",
        token_already_embedded=bool(saved_token),
    )


def _format_mcv_reply(reply: str, *, max_len: int = 3500) -> str:
    """Wrap code blocks in <pre> and HTML-escape the rest. Truncate gently."""
    out: list[str] = []
    i = 0
    while True:
        match = re.search(r"```([a-zA-Z0-9+_-]*)\n?", reply[i:])
        if not match:
            out.append(html.escape(reply[i:], quote=False))
            break
        start = i + match.start()
        end_match = re.search(r"```", reply[start + len(match.group(0)):])
        if not end_match:
            out.append(html.escape(reply[i:], quote=False))
            break
        out.append(html.escape(reply[i:start], quote=False))
        block = reply[start + len(match.group(0)):start + len(match.group(0)) + end_match.start()]
        out.append("<pre>" + html.escape(block, quote=False) + "</pre>")
        i = start + len(match.group(0)) + end_match.end()
    body = "".join(out).strip()
    if len(body) > max_len:
        body = body[:max_len] + "\n…(تم تقصير الرد)"
    return body or "🤖 (لا رد)"


async def _mcv_generate_new_bot(client: TgClient, chat_id: int, uid: int, lang: str,
                                 description: str,
                                 *, embed_token: str | None = None) -> None:
    thinking = await client.send_message(chat_id,
        "🤖 <i>MCV بيكتب بوت كامل لك… ممكن ياخد ١٠ ثواني.</i>")
    try:
        file_name, code = await generate_bot(description, embed_token=embed_token)
    except MCVError as exc:
        await client.edit_message_text(chat_id, thinking["message_id"],
                                       f"❌ MCV ما عرفش يولّد الكود: <code>{html.escape(str(exc), quote=False)}</code>",
                                       reply_markup=kb_back_main(lang))
        return
    await _present_mcv_generated_file(client, chat_id, uid, lang,
                                       file_name=file_name, code=code,
                                       message_id=thinking["message_id"],
                                       intent="new",
                                       token_already_embedded=bool(embed_token))


async def _mcv_wizard_step(client: TgClient, chat_id: int, uid: int, lang: str,
                           text: str, pending: dict[str, Any]) -> None:
    """Multi-turn requirements-gathering wizard for new-bot creation.

    Stages:
      * ``purpose``      — first time: ask user "what does the bot do?"
      * ``feature_loop`` — keep asking "anything else?" until they say
                            خلاص / كده تمام / similar.
      * ``await_token``  — once features collected, prompt for the
                            Telegram BOT_TOKEN. We embed it directly
                            into the generated code so the user doesn't
                            have to paste it manually anywhere.
    """
    if is_exit_phrase(text):
        await pop_pending(uid)
        await client.send_message(chat_id,
            "👋 طيب يا معلم. لو احتجتني تاني اضغط 🔴 MCV.",
            reply_markup=kb_back_main(lang))
        return

    stage = pending.get("stage", "purpose")
    features: list[str] = list(pending.get("features") or [])

    # Stage 1 — capture the bot's overall idea.
    if stage == "purpose":
        if len(text) < 3:
            await client.send_message(chat_id,
                "🤔 وصف الفكرة قصير شوية — قولي بجملة كاملة بيعمل إيه.")
            return
        features.append(text)
        pending["features"] = features
        pending["purpose"] = text
        pending["stage"] = "feature_loop"
        await set_pending(uid, pending)
        # Friendly turn that prompts for the first feature.
        await client.send_message(
            chat_id,
            f"💡 <b>تمام، البوت هيكون:</b> «{html.escape(text, quote=False)}»\n\n"
            "دلوقتي قولي عاوز إيه من المميزات. مثلاً:\n"
            "• «قاعدة بيانات للمستخدمين»\n"
            "• «زر إحصائيات»\n"
            "• «إشعار للأدمن عند حدث معين»\n\n"
            "كل ميزة في رسالة لوحدها، ولما تخلص اكتب <b>خلاص</b> أو "
            "<b>كده تمام</b> ✨",
        )
        return

    # Stage 2 — looping for features. Done phrase ⇒ jump to token prompt.
    if stage == "feature_loop":
        if is_done_phrase(text):
            if len(features) <= 1:
                # We only have the original idea — that's fine, build it.
                pass
            pending["stage"] = "await_token"
            await set_pending(uid, pending)
            sample_feats = "\n".join(f"• {html.escape(f, quote=False)}"
                                       for f in features[1:5]) or "<i>بدون مميزات إضافية</i>"
            await client.send_message(
                chat_id,
                "✅ <b>تمام، خلاص!</b>\n\n"
                f"<b>فكرة:</b> {html.escape(features[0], quote=False)}\n"
                f"<b>المميزات:</b>\n{sample_feats}\n\n"
                "🔑 ابعتلي دلوقتي <b>توكن البوت</b> من @BotFather "
                "(اللي شكله <code>123456:ABC...</code>). هحطه أوتوماتيك "
                "جوه الكود وأشغلك البوت على طول.\n\n"
                "اكتب <b>خروج</b> للإلغاء.",
            )
            return

        # Treat the message as a new feature.
        features.append(text)
        pending["features"] = features
        await set_pending(uid, pending)
        ack = await wizard_acknowledge(text, len(features) - 1)
        await client.send_message(chat_id, html.escape(ack, quote=False))
        return

    # Stage 3 — user pastes the Telegram bot token. We validate it,
    # embed it into the freshly-generated code, and onboard the bot.
    if stage == "await_token":
        token_candidate = text.strip()
        info = await validate_token(token_candidate)
        if not info:
            await client.send_message(
                chat_id,
                "❌ التوكن ده مش شغّال. تأكد إنك ناسخه من @BotFather صح "
                "أو اكتب <b>خروج</b> للإلغاء.",
            )
            return
        bot_username = info.get("username", "")
        # Compose a richer description we can feed into ``generate_bot``.
        description = "\n".join([
            f"الفكرة الأساسية: {features[0]}",
            *(f"ميزة: {f}" for f in features[1:]),
        ])
        await pop_pending(uid)  # clear before long AI call
        # Stage the generated file & onboard immediately using the helper
        # that already knows how to host an MCV-built draft.
        await _mcv_build_and_host(
            client, chat_id, uid, lang,
            description=description,
            token=token_candidate,
            bot_username=bot_username,
        )
        return

    # Unknown stage — reset to purpose.
    pending["stage"] = "purpose"
    await set_pending(uid, pending)
    await client.send_message(chat_id, "🔄 يلا نبدأ من الأول — قولي البوت يعمل إيه؟")


async def _mcv_build_and_host(
    client: TgClient,
    chat_id: int,
    uid: int,
    lang: str,
    *,
    description: str,
    token: str,
    bot_username: str,
) -> None:
    """Generate the bot file, send it to the user, and ask for run consent.

    New flow (May 2026):

    1. MCV writes the code (with token embedded) and saves it to disk.
    2. We send the file to the user as a Telegram document so they can
       inspect / download / share it before anything runs.
    3. We ask "تشغّله ولا لا؟" via inline buttons. The actual hosting +
       polling only happens after the user clicks ✅ شغّل البوت.
    4. If the user says no, the file stays on disk under their bots dir
       but no HostedBot row is created and no process is spawned.

    The pending state ``kind="mcv_await_run"`` carries everything we
    need to resume from the callback.
    """
    thinking = await client.send_message(
        chat_id,
        "🤖 <i>MCV بيكتب البوت ويحط التوكن… استنى ثانية.</i>",
    )
    try:
        file_name, code = await generate_bot(description, embed_token=token)
    except MCVError as exc:
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            f"❌ MCV ما عرفش يولّد الكود: <code>{html.escape(str(exc), quote=False)}</code>",
            reply_markup=kb_back_main(lang),
        )
        return

    # Re-scan to make sure the AI didn't slip anything dangerous in.
    from .security_scan import scan_text as _scan_text

    scan = _scan_text(code, "python")
    if not scan.safe:
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            "❌ <b>الكود اللي MCV عمله رفضه الفحص الأمني.</b>\n\n"
            f"{scan.summary()}",
            reply_markup=kb_back_main(lang),
        )
        return

    # Persist into the user's regular bots tree so the existing runner
    # can supervise it like any other upload if the user opts in.
    bots_root = Path(get_settings().bots_path) / str(uid)
    bots_root.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-_.]", "_", file_name)
    sub_dir = bots_root / f"{uuid.uuid4().hex[:8]}_{Path(safe_name).stem}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    file_path = sub_dir / safe_name
    file_path.write_text(code, encoding="utf-8")

    # Friendly summary stats so the user can sanity-check the AI's work.
    num_handlers = code.count("@bot.message_handler") + code.count(
        "@bot.callback_query_handler",
    )
    num_lines = len(code.splitlines())
    has_polling = "infinity_polling" in code
    badge_polling = "✅" if has_polling else "⚠️"

    caption = (
        "📦 <b>البوت جاهز للتجربة!</b>\n\n"
        f"📄 الاسم: <code>{html.escape(safe_name, quote=False)}</code>\n"
        f"🧮 السطور: <code>{num_lines}</code>\n"
        f"🧩 الـ Handlers: <code>{num_handlers}</code>\n"
        f"🔄 Polling: {badge_polling}\n"
        f"🤖 البوت: <b>@{html.escape(bot_username, quote=False)}</b>"
    )

    # Send the file itself so the user can read it.
    try:
        await client.send_document(chat_id, str(file_path), caption=caption)
    except Exception as exc:  # noqa: BLE001
        logger.warning("send_document for wizard bot failed: %s", exc)
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            "⚠️ البوت اتعمل بس Telegram رفض إرسال الملف. "
            "حاول تاني أو راجع اللوج.",
            reply_markup=kb_back_main(lang),
        )
        return

    # Update the original "thinking..." message into the confirm prompt.
    confirm_kb = inline_kb([
        [Btn(text="✅ شغّل البوت", callback_data="mcv_run_yes", color="green")],
        [Btn(text="✏️ عدّل قبل التشغيل", callback_data="mcv_run_edit", color="blue")],
        [Btn(text="❌ سيبه من غير تشغيل", callback_data="mcv_run_no", color="red")],
    ])
    await client.edit_message_text(
        chat_id, thinking["message_id"],
        "🤔 <b>تشغّله دلوقتي؟</b>\n\n"
        "• <b>✅ شغّل البوت</b> — نسجّل البوت ونشغّله على طول.\n"
        "• <b>✏️ عدّل قبل التشغيل</b> — رد بأي تعديل وأنا أعدّله بالـ AI.\n"
        "• <b>❌ سيبه</b> — الملف بيفضل محفوظ بس مش بيشتغل.",
        reply_markup=confirm_kb,
    )

    # Store everything we need to resume from the callback.
    await set_pending(uid, {
        "kind": "mcv_await_run",
        "file_path": str(file_path),
        "file_name": safe_name,
        "token": token,
        "bot_username": bot_username,
        "description": description,
    })
    await audit(uid, "mcv_wizard_bot_drafted",
                f"file={safe_name} @={bot_username} lines={num_lines} handlers={num_handlers}")


async def _mcv_host_drafted_bot(
    client: TgClient,
    chat_id: int,
    uid: int,
    lang: str,
    *,
    file_path: str,
    file_name: str,
    token: str,
    bot_username: str,
    description: str,
) -> None:
    """Onboard a wizard-drafted bot file and start it (polling).

    Called after the user confirms via the inline buttons added in
    :func:`_mcv_build_and_host`. The file is already on disk; we just
    add the HostedBot row, install deps, start the supervisor, and
    report status.
    """
    thinking = await client.send_message(
        chat_id, "📦 بنصّب مكتبات البوت…",
    )
    try:
        await install_dependencies(language="python", file_path=file_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dep install for wizard bot failed: %s", exc)

    tk_hash = token_hash(token)
    b = HostedBot(
        owner_id=uid,
        name=file_name,
        language="python",
        file_path=file_path,
        token_encrypted=encrypt_token(token),
        token_hash=tk_hash,
        bot_username=bot_username,
        tier=1,
        webhook_url=None,
        use_webhook=False,
    )
    try:
        b = await add_hosted_bot(b)
    except ValueError:
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            "❌ التوكن ده مرفوع بالفعل ببوت تاني. مينفعش نشغّل نفس التوكن "
            "مرتين على نفس الاستضافة.",
            reply_markup=kb_back_main(lang),
        )
        return

    runner = get_runner()
    result = await runner.start_supervised(
        bot_id=b.id, language="python", file_path=file_path,
        token=token, port=None, webhook_url=None,
    )
    from .repo import update_bot_status

    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            f"⚠️ البوت اتسجّل، بس بدأ بصراخ:\n<code>{html.escape(result.error, quote=False)}</code>\n\n"
            "روح تبويب 🤖 بوتاتي عشان تشوف اللوج وتعدّل.",
            reply_markup=kb_back_main(lang),
        )
        return
    await update_bot_status(b.id, status="running", pid=result.pid,
                            last_started_at=dt.datetime.utcnow(),
                            restart_count_inc=True)
    # Drop any old webhook so polling works cleanly.
    try:
        from .telegram_api import TgClient as Cli
        async with Cli(token, timeout=15.0) as tcli:
            await tcli.delete_webhook(drop_pending_updates=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_webhook for wizard bot failed: %s", exc)

    await client.edit_message_text(
        chat_id, thinking["message_id"],
        "🎉 <b>البوت شغّال!</b>\n\n"
        f"🤖 <b>@{html.escape(bot_username, quote=False)}</b>\n"
        f"📄 ملف: <code>{html.escape(file_name, quote=False)}</code>\n"
        f"🔌 وضع: ⚡ Polling\n\n"
        "ادخل عليه دلوقتي وابعتله <code>/start</code> 👌\n\n"
        "تقدر تعدّل عليه أو تشوف اللوج من تبويب 🤖 بوتاتي.",
        reply_markup=kb_back_main(lang),
    )
    await audit(uid, "mcv_wizard_bot_created",
                f"id={b.id} @={bot_username} features={len(description.splitlines())}")


async def _mcv_edit_drafted_file(
    client: TgClient,
    chat_id: int,
    uid: int,
    lang: str,
    *,
    edit_request: str,
    pending: dict[str, Any],
) -> None:
    """Apply an AI edit to a drafted (but not yet hosted) wizard file.

    The user clicked ✏️ on the run-confirm prompt, then typed an
    instruction. We feed (current file content + instruction) to
    :func:`modify_bot_code`, overwrite the file, re-send it, and ask for
    the run confirmation again.
    """
    if is_exit_phrase(edit_request):
        await pop_pending(uid)
        await client.send_message(
            chat_id,
            "👋 طيب يا معلم — سيبت التعديل. تقدر ترجع تشغّل البوت من تبويب "
            "🤖 بوتاتي بعد ما ترفعه يدوياً.",
            reply_markup=kb_back_main(lang),
        )
        return

    file_path = str(pending.get("file_path") or "")
    file_name = str(pending.get("file_name") or "bot.py")
    token = str(pending.get("token") or "")
    bot_username = str(pending.get("bot_username") or "")
    description = str(pending.get("description") or "")

    if not file_path or not Path(file_path).exists():
        await pop_pending(uid)
        await client.send_message(
            chat_id, "❌ ما لقيتش ملف الـ draft. ابدأ من جديد من 🔴 MCV.",
            reply_markup=kb_back_main(lang),
        )
        return

    thinking = await client.send_message(
        chat_id, "✏️ <i>MCV بيعدّل الكود حسب طلبك… استنى ثانية.</i>",
    )
    try:
        current_code = Path(file_path).read_text(encoding="utf-8")
    except OSError as exc:
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            f"❌ ما عرفتش أقرا الملف: <code>{html.escape(str(exc), quote=False)}</code>",
            reply_markup=kb_back_main(lang),
        )
        return

    try:
        new_code = await modify_bot_code(
            current_code, instructions=edit_request, language="python",
        )
    except MCVError as exc:
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            f"❌ MCV ما عرفش يعدّل الكود: <code>{html.escape(str(exc), quote=False)}</code>",
            reply_markup=kb_back_main(lang),
        )
        return

    # Re-embed the token in case the edit lost it.
    from .ai_assistant import _embed_token_into_code
    new_code = _embed_token_into_code(new_code, token)

    # Re-run the security scan on the edited code too.
    from .security_scan import scan_text as _scan_text
    scan = _scan_text(new_code, "python")
    if not scan.safe:
        await client.edit_message_text(
            chat_id, thinking["message_id"],
            "❌ <b>التعديل اللي عمله MCV رفضه الفحص الأمني.</b>\n\n"
            f"{scan.summary()}",
            reply_markup=kb_back_main(lang),
        )
        return

    Path(file_path).write_text(new_code, encoding="utf-8")

    num_handlers = new_code.count("@bot.message_handler") + new_code.count(
        "@bot.callback_query_handler",
    )
    num_lines = len(new_code.splitlines())
    has_polling = "infinity_polling" in new_code
    badge_polling = "✅" if has_polling else "⚠️"

    caption = (
        "📝 <b>الكود اتعدّل!</b>\n\n"
        f"📄 الاسم: <code>{html.escape(file_name, quote=False)}</code>\n"
        f"🧮 السطور: <code>{num_lines}</code>\n"
        f"🧩 الـ Handlers: <code>{num_handlers}</code>\n"
        f"🔄 Polling: {badge_polling}"
    )
    try:
        await client.send_document(chat_id, file_path, caption=caption)
    except Exception as exc:  # noqa: BLE001
        logger.warning("send_document for edited bot failed: %s", exc)

    confirm_kb = inline_kb([
        [Btn(text="✅ شغّل البوت", callback_data="mcv_run_yes", color="green")],
        [Btn(text="✏️ عدّل تاني", callback_data="mcv_run_edit", color="blue")],
        [Btn(text="❌ سيبه", callback_data="mcv_run_no", color="red")],
    ])
    await client.edit_message_text(
        chat_id, thinking["message_id"],
        "🤔 <b>تشغّله دلوقتي؟</b>\n\n"
        "اختار من تحت ايه اللي تحبه:",
        reply_markup=confirm_kb,
    )
    # Refresh pending state — keep the same payload but reset the stage
    # so plain text isn't treated as another edit prompt.
    pending["stage"] = "await_run"
    pending["description"] = description
    pending["bot_username"] = bot_username
    await set_pending(uid, pending)


async def _mcv_edit_existing_bot(client: TgClient, chat_id: int, uid: int, lang: str,
                                  *, bot_id: int, instructions: str) -> None:
    b = await get_bot(bot_id)
    if not b or (b.owner_id != uid and not await is_admin_uid(uid)):
        await client.send_message(chat_id, "❌ البوت ده مش ملكك أو محذوف.")
        return
    try:
        source = Path(b.file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        await client.send_message(chat_id, f"❌ ما عرفتش أقرا الملف: {exc}")
        return
    thinking = await client.send_message(chat_id,
        f"✏️ <i>MCV بيعدّل {html.escape(b.name, quote=False)}…</i>")
    try:
        new_code = await modify_bot_code(source, instructions=instructions, language=b.language)
    except MCVError as exc:
        await client.edit_message_text(chat_id, thinking["message_id"],
                                       f"❌ MCV ما عرفش يعدّل: <code>{html.escape(str(exc), quote=False)}</code>",
                                       reply_markup=kb_back_main(lang))
        return
    # Use the same file name as the original (we want to overwrite on "run").
    await _present_mcv_generated_file(
        client, chat_id, uid, lang,
        file_name=b.name, code=new_code,
        message_id=thinking["message_id"],
        intent="edit", target_bot_id=bot_id,
    )


async def _present_mcv_generated_file(client: TgClient, chat_id: int, uid: int, lang: str,
                                       *, file_name: str, code: str,
                                       message_id: int,
                                       intent: str = "new",
                                       target_bot_id: int | None = None,
                                       token_already_embedded: bool = False) -> None:
    """Save the AI-generated code to a draft file, send it to the user,
    and offer Run / Save-only / Cancel buttons."""
    # Stage the draft on disk in a per-user scratch dir.
    drafts_dir = Path(get_settings().data_path) / "mcv_drafts" / str(uid)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    # Always end up with .py — MCV currently only generates Python.
    base = re.sub(r"[^a-zA-Z0-9_.-]", "_", file_name).strip("._") or "mcv_bot"
    if not base.endswith(".py"):
        base += ".py"
    stamp = uuid.uuid4().hex[:8]
    draft_path = drafts_dir / f"{stamp}_{base}"
    draft_path.write_text(code, encoding="utf-8")

    # Stash metadata for the follow-up Run/Cancel callback.
    _mcv_drafts[uid] = {
        "path": str(draft_path),
        "name": base,
        "intent": intent,
        "target_bot_id": target_bot_id,
    }

    # Send the file. Then update the original "thinking" message with a
    # short confirmation + action buttons.
    try:
        await client.send_document(chat_id, str(draft_path), caption=f"📄 <b>{html.escape(base, quote=False)}</b>")
    except TelegramError as exc:
        logger.warning("send_document for MCV draft failed: %s", exc)

    kb = inline_kb([
        [Btn(t(lang, "btn_run_file"), callback_data="mcvrun_run", color="green"),
         Btn(t(lang, "btn_save_only"), callback_data="mcvrun_save", color="blue")],
        [Btn(t(lang, "btn_cancel"), callback_data="mcvrun_cancel", color="red")],
    ])
    body = (
        "✅ <b>MCV جهّز الملف.</b>\n\n"
        f"📄 {html.escape(base, quote=False)}\n\n"
        + ("هتعدّل البوت الموجود وتعيد تشغيله؟ ولا تحمّل الملف بس؟"
           if intent == "edit"
           else "تحب أرفعه وأشغّله على المنصة، ولا تحمّله بس وتشتغل عليه إنت؟")
    )
    try:
        await client.edit_message_text(chat_id, message_id, body, reply_markup=kb)
    except TelegramError:
        await client.send_message(chat_id, body, reply_markup=kb)


async def _handle_mcv_generated(client: TgClient, cb: dict, u, lang: str, action: str) -> None:
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    uid = int(cb["from"]["id"])

    async def ack(text: str | None = None, alert: bool = False) -> None:
        with contextlib.suppress(Exception):
            await client.answer_callback_query(cb["id"], text=text, show_alert=alert)

    draft = _mcv_drafts.pop(uid, None)
    if not draft:
        await ack("انتهت الجلسة", alert=True)
        return

    if action == "cancel":
        with contextlib.suppress(Exception):
            os.remove(draft["path"])
        await ack("تم الإلغاء")
        await client.edit_message_text(chat_id, msg_id, "❌ تم إلغاء الملف.",
                                       reply_markup=kb_back_main(lang))
        return

    if action == "save":
        await ack("✅ تم الحفظ")
        await client.edit_message_text(chat_id, msg_id,
            "💾 <b>اتحفظ.</b> الملف فوق فوق، حمّله من تلجرام.",
            reply_markup=kb_back_main(lang))
        return

    if action == "run":
        intent = draft.get("intent", "new")
        await ack("⏳ بشغّل…")
        if intent == "edit" and draft.get("target_bot_id"):
            await _run_mcv_edit_apply(client, chat_id, msg_id, u, lang,
                                       bot_id=int(draft["target_bot_id"]),
                                       new_code_path=draft["path"])
        else:
            await _run_mcv_upload_new(client, chat_id, msg_id, u, lang,
                                       draft_path=draft["path"], name=draft["name"])
        return


async def _run_mcv_edit_apply(client: TgClient, chat_id: int, message_id: int, u,
                               lang: str, *, bot_id: int, new_code_path: str) -> None:
    """Overwrite a hosted bot's file with the AI-edited version and restart."""
    b = await get_bot(bot_id)
    if not b or (b.owner_id != u.user_id and not await is_admin_uid(u.user_id)):
        await client.send_message(chat_id, "❌ البوت ده مش ملكك أو محذوف.")
        return
    # Security re-scan the modified file before saving — admins skip the
    # check to avoid blocking on AI false positives for their own bots.
    if not await is_admin_uid(u.user_id):
        from .security_scan import scan_file as _scan_file

        scan = _scan_file(new_code_path, b.language)
        if not scan.safe:
            await client.edit_message_text(
                chat_id, message_id,
                "❌ <b>MCV عدّل الكود لكن الفحص الأمني رفضه.</b>\n\n"
                f"{scan.summary()}",
                reply_markup=kb_back_main(lang),
            )
            return
    try:
        new_code = Path(new_code_path).read_text(encoding="utf-8", errors="replace")
        Path(b.file_path).write_text(new_code, encoding="utf-8")
    except OSError as exc:
        await client.send_message(chat_id, f"❌ ما عرفتش أحفظ الملف: {exc}")
        return
    # Re-install deps in case the AI introduced new imports.
    await client.edit_message_text(chat_id, message_id,
        "📦 بنصّب أي مكتبات جديدة وأعيد التشغيل…")
    try:
        await install_dependencies(language=b.language, file_path=b.file_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dep install after MCV edit failed: %s", exc)
    # Restart the supervised process.
    from .repo import update_bot_status
    from .runner import get_runner
    from .security import decrypt_token

    runner = get_runner()
    await runner.stop(b.id)
    await asyncio.sleep(0.4)
    token = decrypt_token(b.token_encrypted)
    used = {hb.port for hb in await list_user_bots(b.owner_id) if hb.port}
    port = b.port or (allocate_port(used) if b.use_webhook else None)
    result = await runner.start_supervised(
        bot_id=b.id, language=b.language, file_path=b.file_path,
        token=token, port=port, webhook_url=b.webhook_url,
    )
    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
        await client.edit_message_text(chat_id, message_id,
            f"💥 البوت اتعدّل لكن قام يصرخ:\n<code>{html.escape(result.error, quote=False)}</code>",
            reply_markup=kb_back_main(lang))
        return
    await update_bot_status(b.id, status="running", pid=result.pid,
                            last_started_at=dt.datetime.utcnow(), restart_count_inc=True)
    with contextlib.suppress(Exception):
        os.remove(new_code_path)
    await client.edit_message_text(chat_id, message_id,
        f"✅ تم تطبيق التعديل وإعادة تشغيل البوت <b>{html.escape(b.name, quote=False)}</b>.",
        reply_markup=kb_back_main(lang))
    await audit(u.user_id, "mcv_edit_bot", f"id={b.id}")


async def _run_mcv_upload_new(client: TgClient, chat_id: int, message_id: int, u,
                               lang: str, *, draft_path: str, name: str) -> None:
    """Take an MCV-generated draft and onboard it as a new hosted bot.

    The user still has to provide their own bot token (we never ship a
    real token from the AI). We prompt for one and finish the upload
    flow from the resulting pending state.
    """
    # We don't yet know the user's bot token — ask them for it.
    await set_pending(u.user_id, {
        "kind": "mcv_new_bot_await_token",
        "draft_path": draft_path,
        "name": name,
    })
    await client.edit_message_text(chat_id, message_id,
        "🔑 <b>طلب توكن البوت</b>\n\n"
        "MCV جهز ملف البوت. ابعتلي دلوقتي <b>توكن البوت</b> اللي عاوز "
        "تشغّله بيه (هتاخده من @BotFather).\n\n"
        "ابعت <code>/cancel</code> لإلغاء.",
        reply_markup=kb_back_main(lang),
    )


async def _apply_bot_token_change(client: TgClient, chat_id: int, uid: int, lang: str,
                                    *, bot_id: int, new_token: str) -> None:
    """Validate ``new_token``, rewrite the token in the file + DB, restart."""
    from .repo import update_bot_token
    from .runner import get_runner
    from .security import decrypt_token

    b = await get_bot(bot_id)
    if not b or (b.owner_id != uid and not await is_admin_uid(uid)):
        await client.send_message(chat_id, "❌ البوت ده مش ملكك أو محذوف.")
        return
    info = await validate_token(new_token)
    if not info:
        await client.send_message(chat_id, "❌ التوكن غير صالح. ابعت /cancel للخروج أو حاول تاني.")
        await set_pending(uid, {"kind": "change_bot_token", "bot_id": bot_id})
        return
    new_username = info.get("username", "")
    # Replace the old token inside the bot's source file (if it appears literally).
    try:
        old_token = decrypt_token(b.token_encrypted)
    except Exception:  # noqa: BLE001
        old_token = ""
    try:
        src = Path(b.file_path).read_text(encoding="utf-8", errors="replace")
        if old_token and old_token in src:
            Path(b.file_path).write_text(src.replace(old_token, new_token), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not patch token in %s: %s", b.file_path, exc)
    # Update DB.
    new_hash = token_hash(new_token)
    try:
        await update_bot_token(b.id, encrypted=encrypt_token(new_token),
                               token_hash=new_hash, bot_username=new_username)
    except ValueError:
        await client.send_message(chat_id,
            "❌ التوكن ده شغال على بوت تاني عندنا. مينفعش نستضيف نفس التوكن مرتين.")
        return
    # Restart the process so the bot picks up the new token.
    runner = get_runner()
    await runner.stop(b.id)
    await asyncio.sleep(0.3)
    used = {hb.port for hb in await list_user_bots(b.owner_id) if hb.port}
    port = b.port or (allocate_port(used) if b.use_webhook else None)
    result = await runner.start_supervised(
        bot_id=b.id, language=b.language, file_path=b.file_path,
        token=new_token, port=port, webhook_url=b.webhook_url,
    )
    from .repo import update_bot_status

    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
        await client.send_message(chat_id,
            f"⚠️ التوكن اتغير لكن التشغيل فشل:\n<code>{html.escape(result.error, quote=False)}</code>")
        return
    await update_bot_status(b.id, status="running", pid=result.pid,
                            last_started_at=dt.datetime.utcnow(), restart_count_inc=True)
    # Re-set the Telegram webhook for the new token if needed.
    if b.use_webhook and b.webhook_url:
        try:
            from .telegram_api import TgClient as Cli

            new_url = b.webhook_url.replace(b.token_hash, new_hash) \
                if b.token_hash and new_hash else b.webhook_url
            async with Cli(new_token, timeout=15.0) as tcli:
                await tcli.set_webhook(url=new_url,
                                       secret_token=get_settings().webhook_secret,
                                       drop_pending_updates=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_webhook on token change failed: %s", exc)
    await client.send_message(chat_id,
        f"✅ <b>التوكن اتغير.</b>\n\n"
        f"🤖 يوزر جديد: @{html.escape(new_username, quote=False)}\n"
        f"📄 ملف: <code>{html.escape(b.name, quote=False)}</code>")
    await audit(uid, "change_bot_token", f"id={b.id} new=@{new_username}")


async def _finalize_mcv_new_bot(client: TgClient, chat_id: int, uid: int, lang: str,
                                  *, draft_path: str, name: str, token: str) -> None:
    """Validate a user-supplied token and onboard an MCV-generated draft."""
    if token.lower() in ("/cancel", "cancel"):
        await client.send_message(chat_id, "❌ تم الإلغاء.")
        return
    info = await validate_token(token)
    if not info:
        await client.send_message(chat_id, "❌ التوكن غير صالح. حاول تاني وابعت /cancel لو عاوز تلغي.")
        await set_pending(uid, {"kind": "mcv_new_bot_await_token",
                                  "draft_path": draft_path, "name": name})
        return
    bot_username = info.get("username", "")
    # Patch the draft to embed the real token before hosting it.
    try:
        code = Path(draft_path).read_text(encoding="utf-8", errors="replace")
        if "REPLACE_ME" in code:
            code = code.replace("REPLACE_ME", token)
        Path(draft_path).write_text(code, encoding="utf-8")
    except OSError as exc:
        await client.send_message(chat_id, f"❌ ما عرفتش أكتب الملف: {exc}")
        return
    # Decide an "owned" final path under the regular bots_storage tree
    # so the existing supervisor can manage it like any other upload.
    bots_root = Path(get_settings().bots_path) / str(uid)
    sub_dir = bots_root / f"{uuid.uuid4().hex[:8]}_{Path(name).stem}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-_.]", "_", name)
    file_path = sub_dir / safe_name
    file_path.write_text(code, encoding="utf-8")
    # Remove the staging draft.
    with contextlib.suppress(Exception):
        os.remove(draft_path)

    # Decide tier — admin gets T5, otherwise the highest one they unlocked.
    is_admin = await is_admin_uid(uid)
    u = await get_user(uid)
    points = u.points if u else 0
    pick_level = 1
    for tier in TIERS:
        if can_use_tier(tier, points or 0, is_vip=bool(u and u.is_vip), is_admin=is_admin):
            pick_level = tier.level

    wait_id = (await client.send_message(chat_id,
        "📦 بتثبت المكتبات وبشغّل البوت…"))["message_id"]

    deps_ok, deps_log = await install_dependencies(language="python", file_path=str(file_path))
    (sub_dir / "deps.log").write_text(deps_log or "", encoding="utf-8")

    tk_hash = token_hash(token)
    b = HostedBot(
        owner_id=uid,
        name=safe_name,
        language="python",
        file_path=str(file_path),
        token_encrypted=encrypt_token(token),
        token_hash=tk_hash,
        bot_username=bot_username,
        tier=pick_level,
        webhook_url=None,
        use_webhook=False,
    )
    try:
        b = await add_hosted_bot(b)
    except ValueError:
        await client.edit_message_text(chat_id, wait_id,
            "❌ هذا التوكن مستخدم بالفعل عند مستخدم آخر.")
        return
    runner = get_runner()
    result = await runner.start_supervised(
        bot_id=b.id, language="python", file_path=str(file_path),
        token=token, port=None, webhook_url=None,
    )
    from .repo import update_bot_status

    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
        status_str = f"crashed: {result.error}"
    else:
        await update_bot_status(b.id, status="running", pid=result.pid,
                                last_started_at=dt.datetime.utcnow())
        status_str = "running"
    await client.edit_message_text(chat_id, wait_id,
        f"✅ <b>تم رفع وتشغيل بوت {html.escape(bot_username, quote=False)}.</b>\n\n"
        f"الحالة: <code>{html.escape(status_str, quote=False)}</code>",
        reply_markup=kb_back_main(lang))
    await audit(uid, "mcv_new_bot_hosted", f"id={b.id} @={bot_username}")


# ===================== AI bot-intel hook ===================== #

async def _post_upload_ai_intel(client: TgClient, chat_id: int, *, file_path: str,
                                  language: str, bot_username: str, file_name: str) -> None:
    """Run an AI intel pass on a freshly uploaded bot and message the user."""
    try:
        src = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        intel = await detect_bot_purpose(src, language=language, file_name=file_name)
    except MCVError as exc:
        logger.info("MCV intel skipped: %s", exc)
        return
    body = (
        f"🔴 <b>تحليل MCV لبوت @{html.escape(bot_username, quote=False)}</b>\n\n"
        + intel.as_html()
    )
    with contextlib.suppress(Exception):
        await client.send_message(chat_id, body, reply_markup=kb_back_main("ar"))


async def _handle_upload_convert_choice(client: TgClient, chat_id: int, uid: int,
                                          lang: str, *, choice: str, st: dict) -> None:
    """Resolve the "convert to Python?" choice during upload.

    ``choice == "yes"`` runs MCV to convert the file to Python and then
    falls through to the polling/webhook picker. ``choice == "no"``
    skips the conversion and goes straight to the picker with the
    original file.
    """
    wait_id = st["wait_message_id"]
    if choice == "yes":
        await client.edit_message_text(chat_id, wait_id,
            "🐍 <i>MCV بيحوّل الكود لـ Python…</i>")
        original_path = Path(st["file_path"])
        try:
            src = original_path.read_text(encoding="utf-8", errors="replace")
            new_code = await transpile_to_python(src, source_lang=st["language"])
        except MCVError as exc:
            await client.edit_message_text(chat_id, wait_id,
                f"❌ MCV فشل في التحويل: <code>{html.escape(str(exc), quote=False)}</code>\n"
                "هتشغّل الملف الأصلي زي ما هو.")
            new_code = None
        if new_code:
            # Replace the file in place with the Python version. Keep the
            # original around as ``<name>.original.<ext>`` for posterity.
            new_path = original_path.with_suffix(".py")
            new_path.write_text(new_code, encoding="utf-8")
            backup = original_path.with_name(original_path.stem + "_original" + original_path.suffix)
            with contextlib.suppress(Exception):
                original_path.rename(backup)
            st["file_path"] = str(new_path)
            st["language"] = "python"
            st["safe_name"] = new_path.name
            st["file_name"] = new_path.name
    # Move to the polling/webhook picker.
    suggested = detect_run_mode(st["file_path"], st["language"])
    await set_pending(uid, {
        "kind": "upload_choose_mode",
        "language": st["language"],
        "file_path": st["file_path"],
        "sub_dir": st["sub_dir"],
        "file_name": st["file_name"],
        "safe_name": st["safe_name"],
        "token": st["token"],
        "bot_username": st["bot_username"],
        "tier_level": st["tier_level"],
        "wait_message_id": wait_id,
        "suggested": suggested,
    })
    polling_label = "⚡ Polling" + (" (مقترح)" if suggested == "polling" else "")
    webhook_label = "🌐 Webhook" + (" (مقترح)" if suggested == "webhook" else "")
    kb = inline_kb([
        [
            Btn(polling_label, callback_data="upmode_polling",
                color="green" if suggested == "polling" else "blue"),
            Btn(webhook_label, callback_data="upmode_webhook",
                color="green" if suggested == "webhook" else "blue"),
        ],
        [Btn("❌ إلغاء", callback_data="upmode_cancel", color="red")],
    ])
    prompt = (
        "🔌 <b>اختر وضع تشغيل البوت:</b>\n\n"
        "<blockquote>"
        "⚡ <b>Polling</b> — البوت يسأل تلجرام للتحديثات بشكل مستمر.\n\n"
        "🌐 <b>Webhook</b> — تلجرام يبعت التحديثات لسيرفرنا مباشرة."
        "</blockquote>\n\n"
        f"📌 <i>المقترح حسب فحص الكود: <b>{suggested}</b></i>"
    )
    await client.edit_message_text(chat_id, wait_id, prompt, reply_markup=kb)
