"""Shared MCV memory + admin-facing lookup tools.

This module gives the MCV assistant:
  * A single, durable conversation memory in Firebase under ``/mcv/conversations``.
  * A "facts" store under ``/mcv/facts/{scope}`` for things MCV should remember
    across sessions (e.g. who the owner is, what each user prefers, system-wide
    notes).
  * A small toolbox of *admin lookup* helpers — given a Telegram user id, MCV
    can pull a profile (DB + Firebase) and summarise behaviour, recent events,
    and quotas. Used to answer admin questions like "tell me about user X".
  * A scheduler for repeating admin actions (currently broadcasts). The admin
    can ask MCV "post every day at 09:00 the message …" and MCV stores a
    schedule entry in Firebase that the bot honours.

All Firebase reads/writes go through :mod:`firebase_sync`. When Firebase is
unconfigured the helpers degrade gracefully (no-op writes, empty reads).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from . import firebase_sync
from .config import get_settings
from .repo import (
    count_referrals,
    get_api_usage,
    get_or_create_api_key,
    get_user,
    list_user_bots,
)

logger = logging.getLogger(__name__)

# Conversation log entries older than this are pruned by the periodic loop.
_CONVO_RETENTION_DAYS = 30


# ---------- conversation memory ---------- #

async def append_message(user_id: int, role: str, content: str,
                          model: str | None = None) -> None:
    """Append one chat turn to the shared MCV conversation memory."""
    fb = firebase_sync.get_client()
    if fb is None:
        return
    await fb.push(f"mcv/conversations/{user_id}", {
        "role": role,
        "content": content[:4000],
        "model": model,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
    })


async def get_recent_messages(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Pull the most recent ``limit`` turns from Firebase for ``user_id``."""
    fb = firebase_sync.get_client()
    if fb is None:
        return []
    raw = await fb.get(f"mcv/conversations/{user_id}")
    if not isinstance(raw, dict):
        return []
    items = sorted(raw.values(), key=lambda r: r.get("ts", ""))
    return items[-limit:]


# ---------- facts ---------- #

async def set_fact(scope: str, key: str, value: Any) -> None:
    """Store a piece of structured memory under ``/mcv/facts/{scope}/{key}``."""
    fb = firebase_sync.get_client()
    if fb is None:
        return
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", key)[:80] or "_"
    await fb.put(f"mcv/facts/{scope}/{safe_key}", {
        "value": value,
        "updated_at": dt.datetime.utcnow().isoformat() + "Z",
    })


async def get_facts(scope: str) -> dict[str, Any]:
    fb = firebase_sync.get_client()
    if fb is None:
        return {}
    raw = await fb.get(f"mcv/facts/{scope}")
    if not isinstance(raw, dict):
        return {}
    return {k: v.get("value") for k, v in raw.items() if isinstance(v, dict)}


async def bootstrap_owner_facts() -> None:
    """Seed ``/mcv/facts/owner`` with the platform's admin IDs at startup.

    This ensures MCV always knows who the owner/admins are without having
    to be told. Idempotent.
    """
    s = get_settings()
    admins = s.admin_id_list
    if not admins:
        return
    await set_fact("owner", "admin_ids", admins)
    await set_fact("owner", "primary_admin_id", admins[0])


# ---------- admin profile lookup ---------- #

async def profile_user(user_id: int) -> dict[str, Any] | None:
    """Build a comprehensive profile of a user, joining DB + Firebase data.

    Returns ``None`` when the user is not known to the platform.
    """
    u = await get_user(user_id)
    if u is None:
        return None
    bots = await list_user_bots(user_id)
    ai_today = await get_api_usage(user_id, "ai")
    hosting_today = await get_api_usage(user_id, "hosting")
    referrals = await count_referrals(user_id)
    ak = await get_or_create_api_key(user_id)
    # Recent events for this user, pulled from Firebase if available.
    fb = firebase_sync.get_client()
    recent_events: list[dict[str, Any]] = []
    if fb is not None:
        raw = await fb.get("events")
        if isinstance(raw, dict):
            recent_events = [
                v for v in raw.values()
                if isinstance(v, dict) and v.get("user_id") == user_id
            ]
            recent_events.sort(key=lambda e: e.get("ts", ""), reverse=True)
            recent_events = recent_events[:20]
    return {
        "user": {
            "user_id": u.user_id,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "language": u.language,
            "is_admin": bool(u.is_admin),
            "is_vip": bool(u.is_vip),
            "is_banned": bool(u.is_banned),
            "points": int(u.points),
            "referral_code": u.referral_code,
            "referrer_id": u.referrer_id,
            "suspicious_attempts": int(u.suspicious_attempts),
            "join_date": u.join_date.isoformat() if u.join_date else None,
            "last_seen": u.last_seen.isoformat() if u.last_seen else None,
        },
        "stats": {
            "bots_count": len(bots),
            "referrals_count": referrals,
            "ai_today": ai_today,
            "hosting_today": hosting_today,
        },
        "bots": [
            {
                "id": b.id,
                "name": b.name,
                "language": b.language,
                "tier": b.tier,
                "status": b.status,
                "bot_username": b.bot_username,
                "use_webhook": bool(b.use_webhook),
                "restart_count": int(b.restart_count),
                "last_error": b.last_error,
            }
            for b in bots
        ],
        "api_key": {
            "label": ak.label,
            "key_masked": (ak.key[:6] + "…" + ak.key[-4:]) if ak.key else "",
            "created_at": ak.created_at.isoformat() if ak.created_at else None,
            "last_used_at": ak.last_used_at.isoformat() if ak.last_used_at else None,
            "is_revoked": bool(ak.is_revoked),
        },
        "recent_events": recent_events,
    }


# ---------- broadcast scheduling ---------- #
#
# An admin asking MCV "post every day at 09:00 the message …" is parsed to
# a structured schedule like:
#   {
#     "kind": "daily",        # daily | once
#     "hour": 9, "minute": 0,
#     "message": "…",
#     "photo_url": null,
#     "enabled": true,
#     "created_by": <admin_user_id>,
#     "created_at": "…",
#   }
# stored under ``/mcv/schedules/{auto_id}``.

_TIME_RE = re.compile(r"\b([0-2]?\d)\s*[:\.]\s*([0-5]\d)\b")
# Match "يومي" / "يوميا" / "يومياً" — also strip optional trailing tashkeel.
_DAILY_KW_RE = re.compile(r"يومي(?:ا|اً|ًا)?|every\s+day|daily", re.I)
# Strip stray Arabic diacritics that survive after keyword removal.
_TASHKEEL_RE = re.compile(r"[\u064B-\u0652\u0670]+")


def parse_schedule_request(text: str) -> dict[str, Any] | None:
    """Try to extract a daily-broadcast schedule from natural-language text.

    Returns the structured dict on success, ``None`` if the text doesn't
    look like a scheduling request.
    """
    if not text:
        return None
    is_daily = bool(_DAILY_KW_RE.search(text))
    m = _TIME_RE.search(text)
    if not is_daily or not m:
        return None
    hour = int(m.group(1)) % 24
    minute = int(m.group(2))
    # Strip the "every day at X" segment to leave the message.
    cleaned = _DAILY_KW_RE.sub("", text)
    cleaned = _TIME_RE.sub("", cleaned)
    cleaned = _TASHKEEL_RE.sub("", cleaned)
    # Common Arabic stitching words ("the message", "post", "every", "at")
    cleaned = re.sub(
        r"(انشر|اعمل|ابعت|منشور|الرسالة|الساعة|بعت|publish|post|the message|at)",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned)
    msg = cleaned.strip(" .،,:\n\t-—")
    if not msg:
        return None
    return {
        "kind": "daily",
        "hour": hour,
        "minute": minute,
        "message": msg,
        "photo_url": None,
        "enabled": True,
    }


async def create_schedule(*, created_by: int, schedule: dict[str, Any]) -> str | None:
    """Persist a schedule entry. Returns the new Firebase key on success."""
    fb = firebase_sync.get_client()
    if fb is None:
        return None
    payload = dict(schedule)
    payload["created_by"] = created_by
    payload["created_at"] = dt.datetime.utcnow().isoformat() + "Z"
    payload["enabled"] = bool(payload.get("enabled", True))
    return await fb.push("mcv/schedules", payload)


async def list_schedules() -> list[dict[str, Any]]:
    fb = firebase_sync.get_client()
    if fb is None:
        return []
    raw = await fb.get("mcv/schedules")
    if not isinstance(raw, dict):
        return []
    out = []
    for k, v in raw.items():
        if isinstance(v, dict):
            v = dict(v)
            v["_id"] = k
            out.append(v)
    return out


async def mark_schedule_fired(key: str, day_iso: str) -> None:
    fb = firebase_sync.get_client()
    if fb is None:
        return
    await fb.patch(f"mcv/schedules/{key}", {"last_fired_day": day_iso})


# ---------- system prompt augmentation ---------- #

async def build_context_block(user_id: int, *, is_admin: bool) -> str:
    """Build the Arabic context snippet appended to the MCV system prompt.

    Includes:
      * who the owner is (so MCV always recognises the admin)
      * the caller's identity + flags + counters
      * recent admin events (for admin callers only)
    """
    facts = await get_facts("owner")
    primary = facts.get("primary_admin_id")
    admins = facts.get("admin_ids") or []
    me = await profile_user(user_id)
    lines: list[str] = []
    lines.append("---- سياق MCV (لا تطّلع عليه للمستخدم إلا لو طلب) ----")
    if primary:
        lines.append(f"المالك الأساسي للمنصة: ID={primary}.")
    if admins:
        lines.append(f"كل الأدمنز: {', '.join(str(a) for a in admins)}.")
    if me:
        u = me["user"]
        st = me["stats"]
        flags = []
        if u.get("is_admin"):
            flags.append("ADMIN")
        if u.get("is_vip"):
            flags.append("VIP")
        if u.get("is_banned"):
            flags.append("BANNED")
        flag_str = " | ".join(flags) or "regular"
        lines.append(
            f"المتصل بك: id={u['user_id']} username=@{u.get('username') or '-'} "
            f"name={u.get('first_name') or '-'} [{flag_str}] "
            f"bots={st['bots_count']} referrals={st['referrals_count']} "
            f"ai_today={st['ai_today']} hosting_today={st['hosting_today']}."
        )
    if is_admin:
        # Surface recent platform events so the admin can ask follow-ups.
        fb = firebase_sync.get_client()
        if fb is not None:
            raw = await fb.get("events")
            if isinstance(raw, dict):
                events = sorted(
                    (v for v in raw.values() if isinstance(v, dict)),
                    key=lambda e: e.get("ts", ""),
                    reverse=True,
                )[:8]
                if events:
                    lines.append("آخر الأحداث على المنصة:")
                    for e in events:
                        lines.append(
                            f"  - {e.get('ts','')} {e.get('action','')} "
                            f"user={e.get('user_id')} payload={e.get('payload')}"
                        )
    lines.append(
        "ملاحظات: لما الأدمن يسأل عن مستخدم بـ id، اطلب من النظام تشغيل أداة "
        "`profile_user` (مدير الجلسة عنده الصلاحية يلبّيها). لو الأدمن طلب جدولة "
        "منشور يومي، استخدم أداة `create_schedule`."
    )
    lines.append("---- نهاية السياق ----")
    return "\n".join(lines)


__all__ = [
    "append_message",
    "get_recent_messages",
    "set_fact",
    "get_facts",
    "bootstrap_owner_facts",
    "profile_user",
    "parse_schedule_request",
    "create_schedule",
    "list_schedules",
    "mark_schedule_fired",
    "build_context_block",
]
