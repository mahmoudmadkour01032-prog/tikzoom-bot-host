"""Firebase Realtime Database sync layer.

Mirrors the platform's SQLite state — users, hosted bots, API keys, daily
usage, audit events, MCV conversations and shared facts — onto a Firebase
Realtime Database so all platform data is durably backed up off-box and
accessible to MCV across sessions.

Designed to be:
  * Optional — if no service account is configured we no-op (the rest of
    the platform keeps working as before).
  * Cheap — every write is a single async ``PATCH`` against the REST API.
  * Resilient — failures only log a warning and never raise.
  * Cross-session — at startup we bulk re-upload the entire current DB
    snapshot so even if we missed a write while the bot was off, the
    Firebase view converges to the authoritative SQLite state.

Service-account access tokens are minted via Google's OAuth2 token URL
using the JWT bearer-token grant. We cache and refresh them transparently.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as dt
import json
import logging
import os
import time
from typing import Any

import httpx

from .db import (
    ApiKey,
    ApiUsage,
    AuditLog,
    HostedBot,
    Referral,
    User,
    get_session_factory,
)

logger = logging.getLogger(__name__)

# OAuth2 scope needed for Firebase RTDB writes.
_SCOPES = "https://www.googleapis.com/auth/firebase.database https://www.googleapis.com/auth/userinfo.email"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclasses.dataclass
class _Token:
    value: str
    expires_at: float


class FirebaseClient:
    """Tiny async REST client for Firebase Realtime Database."""

    def __init__(self, service_account_json: str, db_url: str | None = None) -> None:
        try:
            self.sa = json.loads(service_account_json)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc
        self.client_email: str = self.sa["client_email"]
        self.private_key_pem: str = self.sa["private_key"]
        self.project_id: str = self.sa.get("project_id", "")
        # Default DB URL pattern: https://<project>-default-rtdb.firebaseio.com OR ...asia-southeast1.firebasedatabase.app
        # The user provided: https://m-c-v-m-bot-default-rtdb.asia-southeast1.firebasedatabase.app/
        env_url = os.environ.get("FIREBASE_DB_URL", "").strip()
        self.db_url = (db_url or env_url or "").rstrip("/")
        if not self.db_url:
            # Fallback derived from project id.
            self.db_url = f"https://{self.project_id}-default-rtdb.firebaseio.com"
        self._http = httpx.AsyncClient(timeout=10.0)
        self._token: _Token | None = None
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        with contextlib_suppress():
            await self._http.aclose()

    # ---------- auth ---------- #
    def _make_jwt(self) -> str:
        """Build a self-signed JWT to exchange for an OAuth2 access token."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        header = {"alg": "RS256", "typ": "JWT"}
        now = int(time.time())
        claims = {
            "iss": self.client_email,
            "scope": _SCOPES,
            "aud": _TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }

        def _b64(o: dict[str, Any]) -> bytes:
            raw = json.dumps(o, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        signing_input = _b64(header) + b"." + _b64(claims)
        key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
        return (signing_input + b"." + sig_b64).decode()

    async def _get_token(self) -> str:
        async with self._token_lock:
            if self._token and self._token.expires_at - time.time() > 60:
                return self._token.value
            jwt_str = self._make_jwt()
            resp = await self._http.post(
                _TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": jwt_str,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            self._token = _Token(
                value=payload["access_token"],
                expires_at=time.time() + int(payload.get("expires_in", 3600)),
            )
            return self._token.value

    # ---------- low-level ---------- #
    async def _request(self, method: str, path: str, *, json_body: Any | None = None) -> httpx.Response:
        token = await self._get_token()
        url = f"{self.db_url}/{path.lstrip('/')}.json"
        headers = {"Authorization": f"Bearer {token}"}
        return await self._http.request(method, url, headers=headers, json=json_body)

    async def patch(self, path: str, data: dict[str, Any]) -> None:
        r = await self._request("PATCH", path, json_body=data)
        if r.status_code >= 400:
            logger.warning("firebase PATCH %s -> %s %s", path, r.status_code, r.text[:200])

    async def put(self, path: str, data: Any) -> None:
        r = await self._request("PUT", path, json_body=data)
        if r.status_code >= 400:
            logger.warning("firebase PUT %s -> %s %s", path, r.status_code, r.text[:200])

    async def push(self, path: str, data: Any) -> str | None:
        """Append a new auto-keyed child. Returns the new key on success."""
        r = await self._request("POST", path, json_body=data)
        if r.status_code >= 400:
            logger.warning("firebase POST %s -> %s %s", path, r.status_code, r.text[:200])
            return None
        try:
            return r.json().get("name")
        except Exception:  # noqa: BLE001
            return None

    async def get(self, path: str) -> Any:
        r = await self._request("GET", path)
        if r.status_code >= 400:
            return None
        return r.json()

    async def delete(self, path: str) -> None:
        r = await self._request("DELETE", path)
        if r.status_code >= 400:
            logger.warning("firebase DELETE %s -> %s %s", path, r.status_code, r.text[:200])


class contextlib_suppress:
    """Minimal async-safe ``contextlib.suppress`` for the close path."""

    def __enter__(self) -> None:
        pass

    def __exit__(self, *exc: object) -> bool:
        return True


# ---------- module-level singleton ---------- #
_client: FirebaseClient | None = None
_disabled: bool = False


def _is_enabled() -> bool:
    return _client is not None and not _disabled


async def init_firebase() -> bool:
    """Initialise the Firebase client from ``FIREBASE_SERVICE_ACCOUNT_JSON``.

    Returns ``True`` if successfully initialised, ``False`` otherwise.
    Safe to call multiple times — re-initialises if env changed.
    """
    global _client, _disabled
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        logger.info("firebase: FIREBASE_SERVICE_ACCOUNT_JSON not set — sync disabled")
        _disabled = True
        return False
    try:
        if _client is not None:
            await _client.close()
        _client = FirebaseClient(raw)
        _disabled = False
        # Verify by writing a heartbeat at /_meta/last_startup.
        await _client.patch("_meta", {
            "last_startup": dt.datetime.utcnow().isoformat() + "Z",
            "project_id": _client.project_id,
        })
        logger.info("firebase: connected to %s as %s", _client.db_url, _client.client_email)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("firebase: init failed: %s", exc)
        _client = None
        _disabled = True
        return False


def get_client() -> FirebaseClient | None:
    return _client


# ---------- fire-and-forget helper ---------- #

def _bg(coro: Any) -> None:
    """Schedule a coroutine without blocking the caller. Logs but never raises."""
    if not _is_enabled():
        return

    async def _run() -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning("firebase background task failed: %s", exc)

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_run())
    except RuntimeError:
        # No running loop — best-effort run synchronously
        try:
            asyncio.run(_run())
        except Exception:  # noqa: BLE001
            pass


# ---------- serialisers ---------- #

def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() + "Z"


def _user_to_dict(u: User) -> dict[str, Any]:
    return {
        "user_id": u.user_id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "language": u.language,
        "contact_phone": u.contact_phone,
        "contact_shared_at": _iso(u.contact_shared_at),
        "is_admin": bool(u.is_admin),
        "is_vip": bool(u.is_vip),
        "vip_expiry": _iso(u.vip_expiry),
        "is_banned": bool(u.is_banned),
        "points": int(u.points),
        "referrer_id": u.referrer_id,
        "referral_code": u.referral_code,
        "suspicious_attempts": int(u.suspicious_attempts),
        "join_date": _iso(u.join_date),
        "last_seen": _iso(u.last_seen),
    }


def _bot_to_dict(b: HostedBot) -> dict[str, Any]:
    return {
        "id": b.id,
        "owner_id": b.owner_id,
        "name": b.name,
        "language": b.language,
        "bot_username": b.bot_username,
        "tier": b.tier,
        "port": b.port,
        "status": b.status,
        "use_webhook": bool(b.use_webhook),
        "restart_count": int(b.restart_count),
        "last_started_at": _iso(b.last_started_at),
        "last_error": b.last_error,
        "created_at": _iso(b.created_at),
    }


def _apikey_to_dict(k: ApiKey) -> dict[str, Any]:
    # We do NOT store the raw key in Firebase — only the masked prefix for
    # admin visibility. The full key remains in SQLite encrypted at rest.
    masked = (k.key[:6] + "…" + k.key[-4:]) if k.key else ""
    return {
        "user_id": k.user_id,
        "label": k.label,
        "key_masked": masked,
        "is_revoked": bool(k.is_revoked),
        "created_at": _iso(k.created_at),
        "last_used_at": _iso(k.last_used_at),
    }


def _usage_to_dict(u: ApiUsage) -> dict[str, Any]:
    return {
        "user_id": u.user_id,
        "day": u.day,
        "category": u.category,
        "count": int(u.count),
    }


def _referral_to_dict(r: Referral) -> dict[str, Any]:
    return {
        "referrer_id": r.referrer_id,
        "referred_id": r.referred_id,
        "created_at": _iso(r.created_at),
    }


def _audit_to_dict(a: AuditLog) -> dict[str, Any]:
    return {
        "user_id": a.user_id,
        "action": a.action,
        "payload": a.payload,
        "created_at": _iso(a.created_at),
    }


# ---------- per-entity push helpers (use these from repo.py) ---------- #

async def push_user(u: User) -> None:
    if not _is_enabled():
        return
    await _client.put(f"users/{u.user_id}", _user_to_dict(u))  # type: ignore[union-attr]


async def push_bot(b: HostedBot) -> None:
    if not _is_enabled() or b.id is None:
        return
    await _client.put(f"bots/{b.id}", _bot_to_dict(b))  # type: ignore[union-attr]


async def delete_bot(bot_id: int) -> None:
    if not _is_enabled():
        return
    await _client.delete(f"bots/{bot_id}")  # type: ignore[union-attr]


async def push_api_key(k: ApiKey) -> None:
    if not _is_enabled() or k.user_id is None:
        return
    await _client.put(f"api_keys/{k.user_id}", _apikey_to_dict(k))  # type: ignore[union-attr]


async def push_usage(u: ApiUsage) -> None:
    if not _is_enabled():
        return
    await _client.put(
        f"usage/{u.day}/{u.category}/{u.user_id}",
        {"count": int(u.count)},
    )  # type: ignore[union-attr]


async def push_referral(r: Referral) -> None:
    if not _is_enabled():
        return
    await _client.put(
        f"referrals/{r.referrer_id}/{r.referred_id}",
        _referral_to_dict(r),
    )  # type: ignore[union-attr]


async def push_event(action: str, payload: dict[str, Any] | None = None,
                     user_id: int | None = None) -> None:
    """Append a structured event to ``/events`` for the admin live feed."""
    if not _is_enabled():
        return
    await _client.push("events", {
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "user_id": user_id,
        "payload": payload or {},
    })  # type: ignore[union-attr]


# Fire-and-forget wrappers — call these from repo.py without awaiting.
def push_user_bg(u: User) -> None: _bg(push_user(u))
def push_bot_bg(b: HostedBot) -> None: _bg(push_bot(b))
def delete_bot_bg(bot_id: int) -> None: _bg(delete_bot(bot_id))
def push_api_key_bg(k: ApiKey) -> None: _bg(push_api_key(k))
def push_usage_bg(u: ApiUsage) -> None: _bg(push_usage(u))
def push_referral_bg(r: Referral) -> None: _bg(push_referral(r))
def push_event_bg(action: str, payload: dict[str, Any] | None = None,
                  user_id: int | None = None) -> None:
    _bg(push_event(action, payload, user_id))


# ---------- bulk startup sync ---------- #

async def bulk_sync_now() -> dict[str, int]:
    """Re-upload the entire SQLite DB to Firebase. Returns counters."""
    if not _is_enabled():
        return {"skipped": 1}
    from sqlmodel import select as sm_select

    counters = {"users": 0, "bots": 0, "api_keys": 0, "usage": 0, "referrals": 0, "audit": 0}
    async with get_session_factory()() as s:
        users = (await s.execute(sm_select(User))).scalars().all()
        for u in users:
            await push_user(u)
            counters["users"] += 1
        bots = (await s.execute(sm_select(HostedBot))).scalars().all()
        for b in bots:
            await push_bot(b)
            counters["bots"] += 1
        keys = (await s.execute(sm_select(ApiKey))).scalars().all()
        for k in keys:
            await push_api_key(k)
            counters["api_keys"] += 1
        usages = (await s.execute(sm_select(ApiUsage))).scalars().all()
        for u2 in usages:
            await push_usage(u2)
            counters["usage"] += 1
        refs = (await s.execute(sm_select(Referral))).scalars().all()
        for r in refs:
            await push_referral(r)
            counters["referrals"] += 1
        # Audit log — only the last 500 entries to bound write volume.
        audits = (
            await s.execute(sm_select(AuditLog).order_by(AuditLog.id.desc()).limit(500))
        ).scalars().all()
        for a in reversed(audits):
            if a.id is not None and _client is not None:
                await _client.put(f"audit/{a.id}", _audit_to_dict(a))
                counters["audit"] += 1
    # Heartbeat update.
    if _client is not None:
        await _client.patch("_meta", {
            "last_full_sync": dt.datetime.utcnow().isoformat() + "Z",
            "counts": counters,
        })
    return counters


async def periodic_resync_loop(interval_seconds: int = 300) -> None:
    """Re-uploads the full snapshot every N seconds — a safety net against
    missed writes. Cheap because the DB is small (hundreds of rows)."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if _is_enabled():
                counts = await bulk_sync_now()
                logger.debug("firebase: periodic resync done: %s", counts)
        except asyncio.CancelledError:  # noqa: PERF203
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("firebase: periodic resync failed: %s", exc)


# Quietly load the local secret file written by deploy/windows/install.ps1.
# This is helpful on the VPS where systemd-style env injection isn't used.
def _maybe_load_secret_file() -> None:
    """If ``FIREBASE_SERVICE_ACCOUNT_FILE`` is set, load that JSON into the env var."""
    path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()
    if not path or os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON"):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"] = fh.read()
    except OSError as exc:
        logger.warning("firebase: failed to load %s: %s", path, exc)


_maybe_load_secret_file()


__all__ = [
    "init_firebase",
    "get_client",
    "bulk_sync_now",
    "periodic_resync_loop",
    "push_user", "push_user_bg",
    "push_bot", "push_bot_bg",
    "delete_bot", "delete_bot_bg",
    "push_api_key", "push_api_key_bg",
    "push_usage", "push_usage_bg",
    "push_referral", "push_referral_bg",
    "push_event", "push_event_bg",
]
