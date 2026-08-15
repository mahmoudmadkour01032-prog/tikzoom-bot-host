"""Database models using SQLModel + async SQLite."""
from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import Field, SQLModel, UniqueConstraint

from .config import get_settings


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


def _new_referral_code() -> str:
    return secrets.token_urlsafe(6)


class User(SQLModel, table=True):
    """A Telegram user known to the platform."""
    __tablename__ = "users"

    user_id: int = Field(primary_key=True)
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language: str = Field(default="ar")
    contact_phone: str | None = None
    contact_shared_at: dt.datetime | None = None

    is_admin: bool = Field(default=False)
    is_vip: bool = Field(default=False)
    vip_expiry: dt.datetime | None = None
    is_banned: bool = Field(default=False)

    points: int = Field(default=0)
    referrer_id: int | None = Field(default=None, foreign_key="users.user_id", index=True)
    referral_code: str = Field(default_factory=_new_referral_code, unique=True, index=True)

    # Counter for files this user uploaded that the scanner flagged as
    # malicious. After 3, the user is auto-banned.
    suspicious_attempts: int = Field(default=0)

    force_sub_verified_at: dt.datetime | None = None
    join_date: dt.datetime = Field(default_factory=_now)
    last_seen: dt.datetime = Field(default_factory=_now)


class Referral(SQLModel, table=True):
    """Records each unique referrer→referred relationship (no duplicates allowed)."""
    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("referrer_id", "referred_id", name="uq_ref_pair"),)

    id: int | None = Field(default=None, primary_key=True)
    referrer_id: int = Field(foreign_key="users.user_id", index=True)
    referred_id: int = Field(foreign_key="users.user_id", unique=True, index=True)  # one referrer per user
    created_at: dt.datetime = Field(default_factory=_now)


class HostedBot(SQLModel, table=True):
    """A user-uploaded bot hosted by the platform."""
    __tablename__ = "hosted_bots"

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="users.user_id", index=True)
    name: str
    language: str  # python | php | node
    file_path: str  # absolute path to the bot file (or entry script)
    token_encrypted: str  # Fernet-encrypted bot token
    token_hash: str = Field(index=True, unique=True)  # SHA-256 hash of plaintext token, used as webhook key
    bot_username: str | None = None
    tier: int = Field(default=1)
    port: int | None = None
    pid: int | None = None
    status: str = Field(default="stopped")  # stopped | running | crashed
    webhook_url: str | None = None
    use_webhook: bool = Field(default=False)
    restart_count: int = Field(default=0)
    last_started_at: dt.datetime | None = None
    last_error: str | None = None
    created_at: dt.datetime = Field(default_factory=_now)


class ForceSubChannel(SQLModel, table=True):
    """Channels users must subscribe to before using the bot."""
    __tablename__ = "force_sub_channels"

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(unique=True)
    title: str | None = None
    invite_link: str | None = None
    created_at: dt.datetime = Field(default_factory=_now)


class Setting(SQLModel, table=True):
    """Generic key/value settings (e.g. main bot token override)."""
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str = Field(default="")
    updated_at: dt.datetime = Field(default_factory=_now)


def _new_api_key() -> str:
    """A long-lived API key in the form ``tk_<urlsafe>``. ~43 chars."""
    return "tk_" + secrets.token_urlsafe(32)


class ApiKey(SQLModel, table=True):
    """Per-user API key for the public REST API."""
    __tablename__ = "api_keys"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.user_id", index=True, unique=True)
    key: str = Field(default_factory=_new_api_key, unique=True, index=True)
    label: str = Field(default="default")
    created_at: dt.datetime = Field(default_factory=_now)
    last_used_at: dt.datetime | None = None
    is_revoked: bool = Field(default=False)


class ApiUsage(SQLModel, table=True):
    """Per-day per-user per-category usage counter.

    Categories are coarse buckets — ``ai``, ``hosting``, ``misc`` — so
    the rate-limiter only needs a single row per (user, day, category).
    """
    __tablename__ = "api_usage"
    __table_args__ = (UniqueConstraint(
        "user_id", "day", "category", name="uq_api_usage_user_day_cat",
    ),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.user_id", index=True)
    day: str = Field(index=True)  # ISO date string YYYY-MM-DD (UTC)
    category: str = Field(index=True)  # "ai" | "hosting" | "misc"
    count: int = Field(default=0)


class AuditLog(SQLModel, table=True):
    """Audit trail for every important action."""
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, index=True)
    action: str = Field(index=True)
    payload: str = Field(default="")
    created_at: dt.datetime = Field(default_factory=_now)


# ---------- engine helpers ---------- #
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().db_url, echo=False, future=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _session_factory


async def init_db() -> None:
    """Create tables. Safe to call multiple times.

    Also runs lightweight inline migrations for columns we've added since the
    initial release. SQLModel's ``create_all`` won't add columns to an
    existing table, so we issue ``ALTER TABLE`` manually for each.
    """
    from sqlalchemy import text

    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        for table, column, sql_type, default in (
            ("users", "suspicious_attempts", "INTEGER", "0"),
        ):
            cols = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in cols.fetchall()}
            if column not in existing:
                await conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {sql_type} "
                    f"NOT NULL DEFAULT {default}",
                ))


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async generator yielding a session and committing on success."""
    async with get_session_factory()() as session:
        yield session
