"""Repository functions: small async helpers that wrap SQLModel CRUD."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from . import firebase_sync as _fb
from .db import (
    ApiKey,
    ApiUsage,
    AuditLog,
    ForceSubChannel,
    HostedBot,
    Referral,
    Setting,
    User,
    _new_api_key,
    get_session_factory,
)

# ---------- users ---------- #

async def upsert_user(*, user_id: int, username: str | None, first_name: str | None,
                      last_name: str | None, language: str | None = None) -> User:
    """Insert-or-update a user. Retries once on race-condition IntegrityError."""
    factory = get_session_factory()
    for attempt in range(2):
        async with factory() as s:
            u = await s.get(User, user_id)
            if u is None:
                u = User(
                    user_id=user_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    language=language or "ar",
                )
                s.add(u)
            else:
                u.username = username
                u.first_name = first_name
                u.last_name = last_name
                if language:
                    u.language = language
                u.last_seen = dt.datetime.utcnow()
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                if attempt == 0:
                    continue
                raise
            await s.refresh(u)
            _fb.push_user_bg(u)
            return u
    raise RuntimeError("upsert_user: unreachable")


async def get_user(user_id: int) -> User | None:
    async with get_session_factory()() as s:
        return await s.get(User, user_id)


async def get_user_by_referral_code(code: str) -> User | None:
    async with get_session_factory()() as s:
        result = await s.execute(select(User).where(User.referral_code == code))
        return result.scalar_one_or_none()


async def set_contact(user_id: int, phone: str) -> None:
    async with get_session_factory()() as s:
        u = await s.get(User, user_id)
        if u:
            u.contact_phone = phone
            u.contact_shared_at = dt.datetime.utcnow()
            await s.commit()
            await s.refresh(u)
            _fb.push_user_bg(u)
            _fb.push_event_bg("contact_shared", {"phone": phone}, user_id=user_id)


async def set_force_sub_verified(user_id: int) -> None:
    async with get_session_factory()() as s:
        u = await s.get(User, user_id)
        if u:
            u.force_sub_verified_at = dt.datetime.utcnow()
            await s.commit()
            await s.refresh(u)
            _fb.push_user_bg(u)


async def set_admin(user_id: int, value: bool) -> None:
    async with get_session_factory()() as s:
        u = await s.get(User, user_id)
        if u:
            u.is_admin = value
            await s.commit()
            await s.refresh(u)
            _fb.push_user_bg(u)
            _fb.push_event_bg("admin_changed", {"value": value}, user_id=user_id)


async def set_vip(user_id: int, value: bool, days: int | None = None) -> None:
    async with get_session_factory()() as s:
        u = await s.get(User, user_id)
        if u:
            u.is_vip = value
            u.vip_expiry = dt.datetime.utcnow() + dt.timedelta(days=days) if (value and days) else None
            await s.commit()
            await s.refresh(u)
            _fb.push_user_bg(u)
            _fb.push_event_bg("vip_changed", {"value": value, "days": days}, user_id=user_id)


async def set_banned(user_id: int, value: bool) -> None:
    async with get_session_factory()() as s:
        u = await s.get(User, user_id)
        if u:
            u.is_banned = value
            await s.commit()
            await s.refresh(u)
            _fb.push_user_bg(u)
            _fb.push_event_bg("banned_changed", {"value": value}, user_id=user_id)


async def record_suspicious_attempt(user_id: int) -> tuple[int, bool]:
    """Increment the suspicious-upload counter for ``user_id``.

    Returns ``(new_count, banned_now)`` — ``banned_now`` is ``True`` if this
    call pushed the user past the auto-ban threshold (3 attempts).
    """
    async with get_session_factory()() as s:
        u = await s.get(User, user_id)
        if not u:
            return 0, False
        u.suspicious_attempts = (u.suspicious_attempts or 0) + 1
        banned_now = False
        if u.suspicious_attempts >= 3 and not u.is_banned:
            u.is_banned = True
            banned_now = True
        await s.commit()
        await s.refresh(u)
        _fb.push_user_bg(u)
        _fb.push_event_bg(
            "suspicious_attempt",
            {"count": u.suspicious_attempts, "banned_now": banned_now},
            user_id=user_id,
        )
        return u.suspicious_attempts, banned_now


async def reset_suspicious_attempts(user_id: int) -> None:
    async with get_session_factory()() as s:
        u = await s.get(User, user_id)
        if u:
            u.suspicious_attempts = 0
            await s.commit()


async def list_users(limit: int = 100, offset: int = 0) -> list[User]:
    async with get_session_factory()() as s:
        result = await s.execute(select(User).order_by(User.join_date.desc()).limit(limit).offset(offset))
        return list(result.scalars().all())


async def search_users(q: str = "", limit: int = 50) -> list[User]:
    """Search users by id (exact) or username/first_name/last_name (substring).

    An empty query returns the most recent ``limit`` users (newest first).
    """
    q = (q or "").strip()
    async with get_session_factory()() as s:
        if not q:
            stmt = select(User).order_by(User.join_date.desc()).limit(limit)
        else:
            try:
                uid_int = int(q)
                stmt = select(User).where(User.user_id == uid_int).limit(limit)
            except ValueError:
                like = f"%{q.lstrip('@').lower()}%"
                stmt = (
                    select(User)
                    .where(or_(
                        func.lower(User.username).like(like),
                        func.lower(User.first_name).like(like),
                        func.lower(User.last_name).like(like),
                    ))
                    .order_by(User.join_date.desc())
                    .limit(limit)
                )
        result = await s.execute(stmt)
        return list(result.scalars().all())


async def count_users() -> int:
    async with get_session_factory()() as s:
        result = await s.execute(select(func.count()).select_from(User))
        return int(result.scalar_one())


# ---------- referrals ---------- #

async def credit_referral(*, referrer_id: int, referred_id: int) -> bool:
    """Adds referral if not already present. Returns True if newly added."""
    if referrer_id == referred_id:
        return False
    async with get_session_factory()() as s:
        existing = await s.execute(
            select(Referral).where(Referral.referred_id == referred_id)
        )
        if existing.scalar_one_or_none() is not None:
            return False
        referrer = await s.get(User, referrer_id)
        referred = await s.get(User, referred_id)
        if not referrer or not referred:
            return False
        referred.referrer_id = referrer_id
        ref = Referral(referrer_id=referrer_id, referred_id=referred_id)
        s.add(ref)
        referrer.points = (referrer.points or 0) + 1
        await s.commit()
        await s.refresh(ref)
        await s.refresh(referrer)
        await s.refresh(referred)
        _fb.push_referral_bg(ref)
        _fb.push_user_bg(referrer)
        _fb.push_user_bg(referred)
        _fb.push_event_bg(
            "referral_credited",
            {"referrer_id": referrer_id, "referred_id": referred_id},
        )
        return True


async def count_referrals(user_id: int) -> int:
    async with get_session_factory()() as s:
        result = await s.execute(select(func.count()).select_from(Referral).where(Referral.referrer_id == user_id))
        return int(result.scalar_one())


# ---------- hosted bots ---------- #

async def add_hosted_bot(bot: HostedBot) -> HostedBot:
    """Insert a HostedBot, or update the existing record if the owner re-uploads
    a bot with the same token (``token_hash``).

    If a bot with the same ``token_hash`` already exists and belongs to the
    same owner, its mutable fields (``file_path``, ``name``, ``language``,
    ``tier``, ``port``, ``webhook_url``, ``use_webhook``, ``token_encrypted``,
    ``bot_username``) are overwritten, the row is marked ``stopped`` and
    ``last_error`` cleared.

    If a bot with the same ``token_hash`` already exists but belongs to a
    *different* owner, a ``ValueError`` is raised so the caller can refuse the
    upload. (Letting two users host the same Telegram bot would let one user
    silently steal traffic from the other.)
    """
    async with get_session_factory()() as s:
        existing = await s.execute(
            select(HostedBot).where(HostedBot.token_hash == bot.token_hash)
        )
        existing_bot = existing.scalar_one_or_none()
        if existing_bot is not None:
            if existing_bot.owner_id != bot.owner_id:
                raise ValueError(
                    "this bot token is already hosted by another user"
                )
            existing_bot.name = bot.name
            existing_bot.language = bot.language
            existing_bot.file_path = bot.file_path
            existing_bot.token_encrypted = bot.token_encrypted
            existing_bot.bot_username = bot.bot_username
            existing_bot.tier = bot.tier
            existing_bot.port = bot.port
            existing_bot.webhook_url = bot.webhook_url
            existing_bot.use_webhook = bot.use_webhook
            existing_bot.status = "stopped"
            existing_bot.last_error = None
            await s.commit()
            await s.refresh(existing_bot)
            _fb.push_bot_bg(existing_bot)
            _fb.push_event_bg("bot_upserted", {"bot_id": existing_bot.id}, user_id=existing_bot.owner_id)
            return existing_bot
        s.add(bot)
        try:
            await s.commit()
        except IntegrityError:
            # race condition: another concurrent upload won the unique-token slot
            await s.rollback()
            existing = await s.execute(
                select(HostedBot).where(HostedBot.token_hash == bot.token_hash)
            )
            existing_bot = existing.scalar_one_or_none()
            if existing_bot is None or existing_bot.owner_id != bot.owner_id:
                raise ValueError(
                    "this bot token is already hosted by another user"
                ) from None
            return existing_bot
        await s.refresh(bot)
        _fb.push_bot_bg(bot)
        _fb.push_event_bg("bot_created", {"bot_id": bot.id}, user_id=bot.owner_id)
        return bot


async def list_user_bots(user_id: int) -> list[HostedBot]:
    async with get_session_factory()() as s:
        result = await s.execute(select(HostedBot).where(HostedBot.owner_id == user_id).order_by(HostedBot.id))
        return list(result.scalars().all())


async def list_all_bots() -> list[HostedBot]:
    async with get_session_factory()() as s:
        result = await s.execute(select(HostedBot).order_by(HostedBot.id))
        return list(result.scalars().all())


async def count_user_bots_in_tier(user_id: int, tier: int) -> int:
    async with get_session_factory()() as s:
        result = await s.execute(
            select(func.count()).select_from(HostedBot).where(
                HostedBot.owner_id == user_id, HostedBot.tier == tier
            )
        )
        return int(result.scalar_one())


async def get_bot_by_token_hash(token_hash: str) -> HostedBot | None:
    async with get_session_factory()() as s:
        result = await s.execute(select(HostedBot).where(HostedBot.token_hash == token_hash))
        return result.scalar_one_or_none()


async def get_bot(bot_id: int) -> HostedBot | None:
    async with get_session_factory()() as s:
        return await s.get(HostedBot, bot_id)


async def update_bot_status(bot_id: int, *, status: str | None = None, pid: int | None = None,
                            last_started_at: dt.datetime | None = None,
                            last_error: str | None = None,
                            restart_count_inc: bool = False) -> None:
    async with get_session_factory()() as s:
        b = await s.get(HostedBot, bot_id)
        if not b:
            return
        if status is not None:
            b.status = status
        if pid is not None:
            b.pid = pid
        if last_started_at is not None:
            b.last_started_at = last_started_at
        if last_error is not None:
            b.last_error = last_error
        if restart_count_inc:
            b.restart_count = (b.restart_count or 0) + 1
        await s.commit()
        await s.refresh(b)
        _fb.push_bot_bg(b)


async def update_bot_token(bot_id: int, *, encrypted: str, token_hash: str,
                            bot_username: str | None = None) -> None:
    """Replace the encrypted token + hash + username for a hosted bot.

    Raises :class:`ValueError` if another *different* hosted bot already
    holds the same ``token_hash``.
    """
    async with get_session_factory()() as s:
        # Refuse the change if the new hash belongs to another bot.
        other = await s.execute(
            select(HostedBot).where(
                HostedBot.token_hash == token_hash,
                HostedBot.id != bot_id,
            )
        )
        if other.scalar_one_or_none() is not None:
            raise ValueError("token already in use by another bot")
        b = await s.get(HostedBot, bot_id)
        if not b:
            return
        b.token_encrypted = encrypted
        b.token_hash = token_hash
        if bot_username is not None:
            b.bot_username = bot_username
        await s.commit()
        await s.refresh(b)
        _fb.push_bot_bg(b)
        _fb.push_event_bg("bot_token_changed", {"bot_id": bot_id}, user_id=b.owner_id)


async def update_bot_mode(bot_id: int, *, use_webhook: bool,
                          webhook_url: str | None = None) -> None:
    """Switch a hosted bot between webhook and polling modes."""
    async with get_session_factory()() as s:
        b = await s.get(HostedBot, bot_id)
        if not b:
            return
        b.use_webhook = use_webhook
        if webhook_url is not None:
            b.webhook_url = webhook_url
        elif not use_webhook:
            b.webhook_url = None
        await s.commit()
        await s.refresh(b)
        _fb.push_bot_bg(b)


async def delete_bot(bot_id: int) -> None:
    owner_id: int | None = None
    async with get_session_factory()() as s:
        b = await s.get(HostedBot, bot_id)
        if b:
            owner_id = b.owner_id
            await s.delete(b)
            await s.commit()
    _fb.delete_bot_bg(bot_id)
    if owner_id is not None:
        _fb.push_event_bg("bot_deleted", {"bot_id": bot_id}, user_id=owner_id)


# ---------- force-sub channels ---------- #

async def list_force_sub_channels() -> list[ForceSubChannel]:
    async with get_session_factory()() as s:
        result = await s.execute(select(ForceSubChannel))
        return list(result.scalars().all())


async def add_force_sub_channel(*, chat_id: int, title: str | None, invite_link: str | None) -> None:
    async with get_session_factory()() as s:
        existing = await s.execute(select(ForceSubChannel).where(ForceSubChannel.chat_id == chat_id))
        if existing.scalar_one_or_none() is None:
            s.add(ForceSubChannel(chat_id=chat_id, title=title, invite_link=invite_link))
            await s.commit()


async def remove_force_sub_channel(chat_id: int) -> None:
    async with get_session_factory()() as s:
        existing = await s.execute(select(ForceSubChannel).where(ForceSubChannel.chat_id == chat_id))
        ch = existing.scalar_one_or_none()
        if ch:
            await s.delete(ch)
            await s.commit()


# ---------- settings (kv store) ---------- #

async def get_setting(key: str, default: str = "") -> str:
    async with get_session_factory()() as s:
        row = await s.get(Setting, key)
        return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    async with get_session_factory()() as s:
        row = await s.get(Setting, key)
        if row:
            row.value = value
            row.updated_at = dt.datetime.utcnow()
        else:
            s.add(Setting(key=key, value=value))
        await s.commit()


# ---------- audit log ---------- #

async def audit(user_id: int | None, action: str, payload: str = "") -> None:
    async with get_session_factory()() as s:
        entry = AuditLog(user_id=user_id, action=action, payload=payload)
        s.add(entry)
        await s.commit()
        await s.refresh(entry)
    _fb.push_event_bg(action, {"payload": payload, "audit_id": entry.id}, user_id=user_id)


# ---------- public API keys ---------- #

async def get_or_create_api_key(user_id: int) -> ApiKey:
    """Return the user's API key, creating one if missing."""
    created = False
    async with get_session_factory()() as s:
        result = await s.execute(select(ApiKey).where(ApiKey.user_id == user_id))
        row = result.scalar_one_or_none()
        if row is None:
            row = ApiKey(user_id=user_id)
            s.add(row)
            try:
                await s.commit()
                created = True
            except IntegrityError:
                await s.rollback()
                result = await s.execute(select(ApiKey).where(ApiKey.user_id == user_id))
                row = result.scalar_one()
            else:
                await s.refresh(row)
        if created:
            _fb.push_api_key_bg(row)
            _fb.push_event_bg("api_key_created", {"label": row.label}, user_id=user_id)
        return row


async def regenerate_api_key(user_id: int) -> ApiKey:
    """Rotate the user's API key — keeps the same row, replaces the secret."""
    async with get_session_factory()() as s:
        result = await s.execute(select(ApiKey).where(ApiKey.user_id == user_id))
        row = result.scalar_one_or_none()
        if row is None:
            row = ApiKey(user_id=user_id)
            s.add(row)
        else:
            row.key = _new_api_key()
            row.is_revoked = False
        await s.commit()
        await s.refresh(row)
    _fb.push_api_key_bg(row)
    _fb.push_event_bg("api_key_regenerated", {}, user_id=user_id)
    return row


async def get_api_key_with_user(key: str) -> tuple[ApiKey, User] | None:
    """Look up an API key and its owner User in one query."""
    async with get_session_factory()() as s:
        result = await s.execute(select(ApiKey).where(ApiKey.key == key))
        ak = result.scalar_one_or_none()
        if ak is None or ak.is_revoked:
            return None
        user = await s.get(User, ak.user_id)
        if user is None:
            return None
        return ak, user


async def touch_api_key(key: str) -> None:
    async with get_session_factory()() as s:
        result = await s.execute(select(ApiKey).where(ApiKey.key == key))
        ak = result.scalar_one_or_none()
        if ak is None:
            return
        ak.last_used_at = dt.datetime.utcnow()
        await s.commit()
        await s.refresh(ak)
        _fb.push_api_key_bg(ak)


# ---------- API usage counters ---------- #

def _today_utc() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


async def get_api_usage(user_id: int, category: str, *, day: str | None = None) -> int:
    """Return today's usage count for ``category``. ``day`` is UTC ISO date."""
    day = day or _today_utc()
    async with get_session_factory()() as s:
        result = await s.execute(
            select(ApiUsage).where(
                ApiUsage.user_id == user_id,
                ApiUsage.day == day,
                ApiUsage.category == category,
            )
        )
        row = result.scalar_one_or_none()
        return int(row.count) if row else 0


async def incr_api_usage(user_id: int, category: str, *, by: int = 1) -> int:
    """Increment today's counter and return the new value."""
    day = _today_utc()
    async with get_session_factory()() as s:
        result = await s.execute(
            select(ApiUsage).where(
                ApiUsage.user_id == user_id,
                ApiUsage.day == day,
                ApiUsage.category == category,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = ApiUsage(user_id=user_id, day=day, category=category, count=by)
            s.add(row)
            try:
                await s.commit()
                await s.refresh(row)
                _fb.push_usage_bg(row)
                return int(row.count)
            except IntegrityError:
                await s.rollback()
                result = await s.execute(
                    select(ApiUsage).where(
                        ApiUsage.user_id == user_id,
                        ApiUsage.day == day,
                        ApiUsage.category == category,
                    )
                )
                row = result.scalar_one()
        row.count = (row.count or 0) + by
        await s.commit()
        await s.refresh(row)
        _fb.push_usage_bg(row)
        return int(row.count)
