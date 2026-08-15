"""Public REST API for TikZoom.

Routes under ``/v1/*`` are intended for *external* developers — they use a
permanent ``tk_…`` API key (Bearer token) instead of Mini App init data.
Every key is owned by a TikZoom user and inherits that user's role:

* ``Free`` accounts:  20 hosting calls/day + 30 AI calls/day baseline.
* ``VIP`` accounts:  100 hosting calls/day + 200 AI calls/day baseline.
* Admins:            unlimited.

The AI baseline can be increased by inviting other users — each *credited*
referral adds ``REFERRAL_BONUS_AI`` extra AI requests/day on top of the
user's tier baseline.

The module is mounted on the main FastAPI app via ``register_public_api``
in ``app/main.py``.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from . import ai_assistant, repo
from .config import get_settings
from .db import HostedBot, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["public-api"])


# --------- quota policy --------- #

FREE_HOSTING_PER_DAY = 20
VIP_HOSTING_PER_DAY = 100
FREE_AI_PER_DAY = 30
VIP_AI_PER_DAY = 200
REFERRAL_BONUS_AI = 25  # each credited referral adds this many AI calls/day


def _hosting_limit(u: User, is_admin: bool) -> int:
    if is_admin:
        return 10**9
    return VIP_HOSTING_PER_DAY if u.is_vip else FREE_HOSTING_PER_DAY


async def _ai_limit(u: User, is_admin: bool) -> int:
    if is_admin:
        return 10**9
    base = VIP_AI_PER_DAY if u.is_vip else FREE_AI_PER_DAY
    refs = await repo.count_referrals(u.user_id)
    return base + refs * REFERRAL_BONUS_AI


# --------- auth helpers --------- #

_BEARER_RE = re.compile(r"^\s*Bearer\s+(\S+)\s*$", re.IGNORECASE)


async def _resolve_key(authorization: str | None,
                       x_api_key: str | None) -> tuple[User, str]:
    """Return (user, raw_key) for a valid Bearer/x-api-key header."""
    key = None
    if x_api_key:
        key = x_api_key.strip()
    elif authorization:
        m = _BEARER_RE.match(authorization or "")
        if m:
            key = m.group(1).strip()
    if not key or not key.startswith("tk_"):
        raise HTTPException(status_code=401, detail={
            "error": "missing_api_key",
            "message": "Provide your API key via 'Authorization: Bearer tk_...'",
        })
    pair = await repo.get_api_key_with_user(key)
    if pair is None:
        raise HTTPException(status_code=401, detail={
            "error": "invalid_api_key",
            "message": "Unknown or revoked API key.",
        })
    _ak, user = pair
    if user.is_banned:
        raise HTTPException(status_code=403, detail={
            "error": "user_banned",
            "message": "This account is banned from the platform.",
        })
    # Best-effort timestamp; never blocks the request.
    try:
        await repo.touch_api_key(key)
    except Exception:  # noqa: BLE001
        pass
    return user, key


async def _is_admin(uid: int) -> bool:
    from .bot_handlers import is_admin_uid

    return await is_admin_uid(uid)


async def _consume_quota(u: User, category: str, *, cost: int = 1) -> dict[str, int]:
    """Increment usage; raise 429 if exceeding limits. Returns
    a dict with ``used`` / ``limit`` / ``remaining`` after consumption."""
    admin = await _is_admin(u.user_id)
    if category == "hosting":
        limit = _hosting_limit(u, admin)
    elif category == "ai":
        limit = await _ai_limit(u, admin)
    else:
        limit = 10**9
    used = await repo.get_api_usage(u.user_id, category)
    if used + cost > limit:
        raise HTTPException(status_code=429, detail={
            "error": "quota_exceeded",
            "message": f"Daily {category} quota exhausted "
                       f"({used}/{limit}).",
            "category": category,
            "used": used,
            "limit": limit,
        })
    new = await repo.incr_api_usage(u.user_id, category, by=cost)
    return {"used": new, "limit": limit, "remaining": max(limit - new, 0)}


# --------- /v1/me --------- #

@router.get("/me", summary="Account info, quotas, today's usage")
async def me(authorization: str | None = Header(default=None),
             x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    admin = await _is_admin(user.user_id)
    hosting_used = await repo.get_api_usage(user.user_id, "hosting")
    ai_used = await repo.get_api_usage(user.user_id, "ai")
    hosting_limit = _hosting_limit(user, admin)
    ai_limit = await _ai_limit(user, admin)
    bots = await repo.list_user_bots(user.user_id)
    return {
        "user": {
            "id": user.user_id,
            "username": user.username,
            "first_name": user.first_name,
            "is_vip": bool(user.is_vip),
            "is_admin": admin,
            "is_banned": bool(user.is_banned),
            "points": user.points or 0,
            "referrals": await repo.count_referrals(user.user_id),
        },
        "quotas": {
            "hosting": {
                "used": hosting_used,
                "limit": hosting_limit,
                "remaining": max(hosting_limit - hosting_used, 0),
                "resets_at": _midnight_utc_iso(),
            },
            "ai": {
                "used": ai_used,
                "limit": ai_limit,
                "remaining": max(ai_limit - ai_used, 0),
                "resets_at": _midnight_utc_iso(),
            },
        },
        "bots": [
            {
                "id": b.id, "name": b.name, "language": b.language,
                "tier": b.tier, "status": b.status,
                "bot_username": b.bot_username,
                "use_webhook": bool(b.use_webhook),
            }
            for b in bots
        ],
    }


def _midnight_utc_iso() -> str:
    now = dt.datetime.utcnow()
    nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.isoformat() + "Z"


# --------- /v1/ai/chat --------- #

@router.post("/ai/chat", summary="Chat with an AI model")
async def ai_chat(request: Request,
                  authorization: str | None = Header(default=None),
                  x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    payload = await _read_json(request)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        # Allow the simpler `prompt` field as an alias.
        prompt = payload.get("prompt") or payload.get("message")
        if isinstance(prompt, str) and prompt.strip():
            messages = [{"role": "user", "content": prompt.strip()}]
        else:
            raise HTTPException(400, detail={
                "error": "bad_request",
                "message": "Provide either `messages` (list) or `prompt` (string).",
            })
    # Validate roles.
    cleaned: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user")).lower()
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content})
    if not cleaned:
        raise HTTPException(400, detail={
            "error": "bad_request",
            "message": "No usable message content.",
        })
    model = payload.get("model")
    system = payload.get("system")
    task = payload.get("task") or "chat"
    if model and not ai_assistant.is_valid_model(model):
        raise HTTPException(400, detail={
            "error": "bad_model",
            "message": f"Unknown model id '{model}'.",
            "valid_models": list(ai_assistant.ALL_MODELS.keys()),
        })

    quota = await _consume_quota(user, "ai")
    # Build history minus the trailing user message (passed separately).
    last_user = cleaned[-1]["content"]
    history = cleaned[:-1]
    try:
        reply = await ai_assistant.chat(
            last_user,
            history=history,
            system=system,
            model=model,
            task=task,
            timeout=180.0,
        )
    except ai_assistant.MCVError as exc:
        raise HTTPException(502, detail={
            "error": "upstream_failure",
            "message": str(exc),
        }) from exc
    return {
        "id": "chat_" + uuid.uuid4().hex[:12],
        "model": model or await ai_assistant.resolve_model_for(task),
        "reply": reply,
        "quota": quota,
    }


# --------- /v1/ai/generate-bot --------- #

@router.post("/ai/generate-bot",
             summary="Generate a complete Python bot from a free-form description")
async def ai_generate_bot(request: Request,
                          authorization: str | None = Header(default=None),
                          x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    payload = await _read_json(request)
    description = (payload.get("description") or payload.get("prompt") or "").strip()
    if not description:
        raise HTTPException(400, detail={
            "error": "bad_request",
            "message": "Provide a `description` of the bot you want.",
        })
    embed_token = (payload.get("token") or "").strip() or None

    quota = await _consume_quota(user, "ai", cost=2)  # bot generation = 2 units
    try:
        name, code = await ai_assistant.generate_bot(
            description, embed_token=embed_token,
        )
    except ai_assistant.MCVError as exc:
        raise HTTPException(502, detail={
            "error": "upstream_failure",
            "message": str(exc),
        }) from exc
    return {
        "id": "gen_" + uuid.uuid4().hex[:12],
        "name": name,
        "language": "python",
        "code": code,
        "lines": code.count("\n") + 1,
        "quota": quota,
    }


# --------- /v1/ai/modify-bot --------- #

@router.post("/ai/modify-bot",
             summary="Modify existing bot code per a free-form instruction")
async def ai_modify_bot(request: Request,
                        authorization: str | None = Header(default=None),
                        x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    payload = await _read_json(request)
    source = payload.get("source") or payload.get("code") or ""
    instructions = payload.get("instructions") or payload.get("prompt") or ""
    language = (payload.get("language") or "python").lower()
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(400, detail={
            "error": "bad_request",
            "message": "Provide the current `source` of the bot.",
        })
    if not isinstance(instructions, str) or not instructions.strip():
        raise HTTPException(400, detail={
            "error": "bad_request",
            "message": "Provide the `instructions` describing the edit.",
        })
    quota = await _consume_quota(user, "ai", cost=2)
    try:
        new_code = await ai_assistant.modify_bot_code(
            source, instructions=instructions, language=language,
        )
    except ai_assistant.MCVError as exc:
        raise HTTPException(502, detail={
            "error": "upstream_failure",
            "message": str(exc),
        }) from exc
    return {
        "id": "mod_" + uuid.uuid4().hex[:12],
        "language": language,
        "code": new_code,
        "lines": new_code.count("\n") + 1,
        "quota": quota,
    }


# --------- /v1/hosting/bots --------- #

@router.get("/hosting/bots", summary="List your hosted bots")
async def hosting_list(authorization: str | None = Header(default=None),
                       x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    bots = await repo.list_user_bots(user.user_id)
    return {
        "bots": [
            {
                "id": b.id, "name": b.name, "language": b.language,
                "tier": b.tier, "status": b.status, "pid": b.pid,
                "bot_username": b.bot_username,
                "use_webhook": bool(b.use_webhook),
                "webhook_url": b.webhook_url,
                "last_started_at": (
                    b.last_started_at.isoformat() + "Z"
                    if b.last_started_at else None
                ),
                "last_error": b.last_error,
                "restart_count": b.restart_count or 0,
                "created_at": b.created_at.isoformat() + "Z",
            }
            for b in bots
        ],
    }


@router.get("/hosting/bots/{bot_id}", summary="Get one hosted bot")
async def hosting_get(bot_id: int,
                      authorization: str | None = Header(default=None),
                      x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    b = await repo.get_bot(bot_id)
    if not b or b.owner_id != user.user_id:
        raise HTTPException(404, detail={"error": "not_found"})
    return {
        "id": b.id, "name": b.name, "language": b.language,
        "tier": b.tier, "status": b.status, "pid": b.pid,
        "bot_username": b.bot_username,
        "use_webhook": bool(b.use_webhook),
        "webhook_url": b.webhook_url,
        "last_error": b.last_error,
        "restart_count": b.restart_count or 0,
    }


@router.post("/hosting/bots",
             summary="Upload a bot file and start hosting it")
async def hosting_upload(
    request: Request,
    file: UploadFile = File(...),
    tier: int = Form(1),
    mode: str = Form("auto"),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    if mode not in {"auto", "polling", "webhook"}:
        raise HTTPException(400, detail={"error": "bad_mode"})
    from .bot_handlers import (
        detect_run_mode,
        public_base_url,
        webhook_url_for_token,
    )
    from .deps import install_dependencies
    from .runner import allocate_port, get_runner
    from .security import encrypt_token, token_hash
    from .telegram_api import TgClient as Cli
    from .tiers import by_level, can_use_tier, max_files_for
    from .token_extract import detect_language, extract_token_from_file, validate_token

    admin = await _is_admin(user.user_id)
    tier_obj = by_level(int(tier))
    if not tier_obj:
        raise HTTPException(400, detail={"error": "bad_tier"})
    if not can_use_tier(tier_obj, user.points or 0,
                        is_vip=user.is_vip, is_admin=admin):
        raise HTTPException(403, detail={"error": "tier_locked"})
    cap = max_files_for(tier_obj, is_vip=user.is_vip, is_admin=admin)
    if await repo.count_user_bots_in_tier(user.user_id, int(tier)) >= cap:
        raise HTTPException(409, detail={"error": "capacity_reached",
                                         "limit": cap})
    quota = await _consume_quota(user, "hosting")

    file_name = file.filename or "unknown.bin"
    language = detect_language(file_name)
    if not language:
        raise HTTPException(400, detail={"error": "unsupported_file_type"})
    bots_root = Path(get_settings().bots_path) / str(user.user_id)
    bots_root.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-_.]", "_", file_name)
    sub_dir = bots_root / f"{uuid.uuid4().hex[:8]}_{Path(safe_name).stem}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    file_path = sub_dir / safe_name
    file_path.write_bytes(await file.read())

    if not admin:
        from .security_scan import scan_file as _scan_file

        scan = _scan_file(str(file_path), language)
        if not scan.safe:
            attempts, banned_now = await repo.record_suspicious_attempt(user.user_id)
            try:
                file_path.unlink()
            except OSError:
                pass
            try:
                sub_dir.rmdir()
            except OSError:
                pass
            raise HTTPException(400, detail={
                "error": "security_scan_failed",
                "attempts": attempts,
                "banned": banned_now,
                "risks": scan.risks,
            })

    token = extract_token_from_file(str(file_path))
    if not token:
        raise HTTPException(400, detail={"error": "no_token_found"})
    info = await validate_token(token)
    if not info:
        raise HTTPException(400, detail={"error": "invalid_token"})
    bot_username = info.get("username", "")

    if mode == "auto":
        resolved = detect_run_mode(str(file_path), language)
    else:
        resolved = mode
    use_webhook = (resolved == "webhook")

    deps_ok, deps_log = await install_dependencies(
        language=language, file_path=str(file_path),
    )
    (sub_dir / "deps.log").write_text(deps_log or "", encoding="utf-8")

    tk_hash = token_hash(token)
    base = await public_base_url()
    webhook_url = webhook_url_for_token(base, tk_hash) if use_webhook else None
    b = HostedBot(
        owner_id=user.user_id,
        name=safe_name,
        language=language,
        file_path=str(file_path),
        token_encrypted=encrypt_token(token),
        token_hash=tk_hash,
        bot_username=bot_username,
        tier=int(tier),
        webhook_url=webhook_url,
        use_webhook=use_webhook,
    )
    try:
        b = await repo.add_hosted_bot(b)
    except ValueError:
        raise HTTPException(409, detail={
            "error": "duplicate_token",
            "message": "This bot token is already hosted by a different user.",
        })
    used = {hb.port for hb in await repo.list_user_bots(user.user_id) if hb.port}
    port = allocate_port(used) if use_webhook else None
    runner = get_runner()
    result = await runner.start_supervised(
        bot_id=b.id, language=language, file_path=str(file_path),
        token=token, port=port, webhook_url=webhook_url,
    )
    if result.error:
        await repo.update_bot_status(b.id, status="crashed",
                                     last_error=result.error)
        status_str = f"crashed: {result.error}"
    else:
        await repo.update_bot_status(
            b.id, status="running", pid=result.pid,
            last_started_at=dt.datetime.utcnow(),
        )
        status_str = "running"
        try:
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
            logger.warning("public api: webhook config failed: %s", exc)
    await repo.audit(user.user_id, "public_api_upload",
                     f"id={b.id} lang={language} tier={tier} mode={resolved}")
    return {
        "id": b.id,
        "bot_username": bot_username,
        "status": status_str,
        "mode": resolved,
        "use_webhook": use_webhook,
        "webhook_url": webhook_url,
        "quota": quota,
    }


@router.delete("/hosting/bots/{bot_id}",
               summary="Stop and delete a hosted bot")
async def hosting_delete(bot_id: int,
                         authorization: str | None = Header(default=None),
                         x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    from .runner import get_runner
    from .telegram_api import TgClient as Cli
    from .security import decrypt_token

    user, _key = await _resolve_key(authorization, x_api_key)
    b = await repo.get_bot(bot_id)
    if not b or b.owner_id != user.user_id:
        raise HTTPException(404, detail={"error": "not_found"})
    await _consume_quota(user, "hosting")
    runner = get_runner()
    await runner.stop(bot_id)
    try:
        tok = decrypt_token(b.token_encrypted)
        async with Cli(tok, timeout=10.0) as tcli:
            await tcli.delete_webhook(drop_pending_updates=True)
    except Exception:  # noqa: BLE001
        pass
    await repo.delete_bot(bot_id)
    return {"ok": True, "deleted_id": bot_id}


@router.post("/hosting/bots/{bot_id}/restart",
             summary="Restart a hosted bot's subprocess")
async def hosting_restart(bot_id: int,
                          authorization: str | None = Header(default=None),
                          x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    from .runner import get_runner
    from .security import decrypt_token

    user, _key = await _resolve_key(authorization, x_api_key)
    b = await repo.get_bot(bot_id)
    if not b or b.owner_id != user.user_id:
        raise HTTPException(404, detail={"error": "not_found"})
    await _consume_quota(user, "hosting")
    runner = get_runner()
    await runner.stop(bot_id)
    try:
        tok = decrypt_token(b.token_encrypted)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, detail={
            "error": "decrypt_failed", "message": str(exc),
        }) from exc
    result = await runner.start_supervised(
        bot_id=b.id, language=b.language, file_path=b.file_path,
        token=tok, port=b.port, webhook_url=b.webhook_url,
    )
    if result.error:
        await repo.update_bot_status(bot_id, status="crashed",
                                     last_error=result.error)
        return {"ok": False, "status": "crashed", "error": result.error}
    await repo.update_bot_status(
        bot_id, status="running", pid=result.pid,
        last_started_at=dt.datetime.utcnow(),
    )
    return {"ok": True, "status": "running", "pid": result.pid}


@router.get("/hosting/bots/{bot_id}/logs",
            summary="Tail the last N lines of a bot's stdout/stderr log")
async def hosting_logs(bot_id: int,
                       lines: int = 200,
                       authorization: str | None = Header(default=None),
                       x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    user, _key = await _resolve_key(authorization, x_api_key)
    b = await repo.get_bot(bot_id)
    if not b or b.owner_id != user.user_id:
        raise HTTPException(404, detail={"error": "not_found"})
    log_path = Path(get_settings().data_path) / "logs" / f"bot_{bot_id}.log"
    if not log_path.exists():
        return PlainTextResponse("")
    try:
        tail = log_path.read_bytes()[-max(2000, int(lines) * 200):]
        text = tail.decode("utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(500, detail={
            "error": "log_read_failed", "message": str(exc),
        }) from exc
    return PlainTextResponse(text)


# --------- /v1/models --------- #

@router.get("/models", summary="List available AI models")
async def list_models(authorization: str | None = Header(default=None),
                      x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
    await _resolve_key(authorization, x_api_key)
    return {
        "models": [
            {
                "key": k,
                "label": label,
                "tier": ai_assistant.MODEL_TIER.get(k, "primary"),
            }
            for k, label in ai_assistant.ALL_MODELS.items()
        ],
    }


# --------- helpers --------- #

async def _read_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.body()
        if not body:
            return {}
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, detail={
            "error": "bad_json", "message": str(exc),
        }) from exc


def register_public_api(app) -> None:
    app.include_router(router)
