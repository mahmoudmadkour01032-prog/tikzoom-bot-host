"""FastAPI entry point: hosts the main bot's webhook + routes hosted bot webhooks + serves the Mini App."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import datetime as dt
import os
import re
import uuid

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .bot_handlers import handle_update
from .config import get_settings
from .db import init_db
from .repo import get_bot_by_token_hash, get_setting
from .runner import get_runner
from .telegram_api import TgClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

app = FastAPI(title="TikZoom Bot Host", version="0.1.0")
_main_client: TgClient | None = None

# Mount the public REST API (Bearer-token + daily quota) under /v1/*.
# Has to happen at import time so the routes are registered before the
# Mini App static mount catches /app/*.
from .public_api import register_public_api  # noqa: E402

register_public_api(app)


# Mini App static directory — mounted at the bottom of the file AFTER all
# `/app/api/...` dynamic routes are registered, so the mount never shadows
# them. (FastAPI mounts intercept any request whose path starts with the
# mount prefix, regardless of declaration order, so we explicitly mount
# under a non-conflicting prefix and re-export ``/app/`` as a redirect.)
STATIC_DIR = Path(__file__).parent / "miniapp"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


async def get_main_token() -> str:
    """Resolve the *current* main bot token: DB override beats env."""
    db_tok = await get_setting("main_bot_token", "")
    return db_tok or get_settings().bot_token


async def get_main_client() -> TgClient:
    global _main_client
    token = await get_main_token()
    if _main_client is None or _main_client.token != token:
        if _main_client is not None:
            await _main_client.close()
        _main_client = TgClient(token)
    return _main_client


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    Path(get_settings().data_path / "logs").mkdir(parents=True, exist_ok=True)
    cli = await get_main_client()
    me = await _safe_get_me(cli)
    if me:
        logger.info("main bot: @%s (id=%s)", me.get("username"), me.get("id"))
        from .repo import set_setting

        await set_setting("main_bot_username", me.get("username", ""))
    # ---- Firebase Realtime Database sync ----
    from . import firebase_sync, mcv_memory
    fb_ok = await firebase_sync.init_firebase()
    if fb_ok:
        # Bulk-upload the current DB snapshot so Firebase reflects reality
        # even if writes were missed while the bot was offline.
        asyncio.create_task(_initial_firebase_sync())
        asyncio.create_task(firebase_sync.periodic_resync_loop(300))
        await mcv_memory.bootstrap_owner_facts()
        # Periodic broadcast scheduler driven by MCV memory.
        asyncio.create_task(_schedule_runner_loop())
    # Re-launch every hosted bot's subprocess and refresh its Telegram webhook
    # with the current PUBLIC_BASE_URL (which may have changed if a tunnel
    # service rotated its URL).
    asyncio.create_task(_reregister_hosted_bots())
    # Refresh the main bot's Mini App menu button to the current tunnel URL.
    asyncio.create_task(_refresh_main_bot_menu())
    # If polling mode is enabled (TIKZOOM_POLL=1), launch the polling loop
    import os as _os
    settings = get_settings()
    if _os.environ.get("TIKZOOM_POLL", "0") == "1":
        asyncio.create_task(_polling_loop(cli))
    logger.info("startup complete (port=%s)", settings.port)


async def _initial_firebase_sync() -> None:
    from . import firebase_sync
    from .notifications import notify_admins_text

    try:
        counters = await firebase_sync.bulk_sync_now()
        logger.info("firebase: initial bulk sync complete: %s", counters)
        # DM the admin a one-line summary so they know sync is alive.
        cli = await get_main_client()
        await notify_admins_text(
            cli,
            "🔥 <b>Firebase sync</b>\n"
            "تم تحميل بيانات المنصة على Firebase RTDB.\n"
            + " · ".join(f"{k}={v}" for k, v in counters.items()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("initial firebase sync failed: %s", exc)


async def _schedule_runner_loop() -> None:
    """Tick once a minute; fire any due daily schedules from ``/mcv/schedules``."""
    from . import mcv_memory
    from .broadcast import send_broadcast

    last_minute_seen: str = ""
    while True:
        try:
            await asyncio.sleep(30)
            now = dt.datetime.utcnow()
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if minute_key == last_minute_seen:
                continue
            last_minute_seen = minute_key
            schedules = await mcv_memory.list_schedules()
            today = now.strftime("%Y-%m-%d")
            for sched in schedules:
                if not sched.get("enabled", True):
                    continue
                if sched.get("kind") != "daily":
                    continue
                if sched.get("last_fired_day") == today:
                    continue
                hour = int(sched.get("hour", 0))
                minute = int(sched.get("minute", 0))
                if now.hour != hour or now.minute != minute:
                    continue
                msg = sched.get("message") or ""
                if not msg:
                    continue
                cli = await get_main_client()
                report = await send_broadcast(cli, text=msg, photo=sched.get("photo_url"))
                await mcv_memory.mark_schedule_fired(sched["_id"], today)
                from .notifications import notify_admins_text
                await notify_admins_text(
                    cli,
                    "⏰ <b>إذاعة مجدولة</b>\n"
                    f"تم إرسال منشور يومي مجدول الساعة {hour:02d}:{minute:02d}.\n"
                    f"المسلَّم: {report.delivered} / فشل: {report.failed}",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("schedule runner: %s", exc)


async def _reregister_hosted_bots() -> None:
    """Restart subprocess for each HostedBot and re-set its Telegram webhook.

    Before launching, we re-install any missing runtime dependencies so a
    bot that was uploaded before the auto-installer existed (or whose venv
    was wiped) still gets its packages restored.
    """
    from .bot_handlers import public_base_url, webhook_url_for_token
    from .deps import install_dependencies
    from .repo import list_all_bots, list_user_bots, update_bot_status
    from .runner import allocate_port, get_runner
    from .security import decrypt_token

    from .bot_handlers import detect_run_mode
    from .repo import update_bot_mode

    base = await public_base_url()
    runner = get_runner()
    for bot in await list_all_bots():
        try:
            tok = decrypt_token(bot.token_encrypted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("decrypt token for bot=%s failed: %s", bot.id, exc)
            continue
        # Install deps first (fast no-op for already-installed targets).
        try:
            ok, log_text = await install_dependencies(
                language=bot.language, file_path=bot.file_path,
            )
            if not ok:
                logger.warning("dep install for bot=%s reported failure: %s",
                               bot.id, (log_text or "")[:300])
        except Exception as exc:  # noqa: BLE001
            logger.warning("dep install crashed for bot=%s: %s", bot.id, exc)
        # Re-detect mode from the bot's source file in case it changed (or
        # the original detector wasn't run at upload time).
        detected_mode = detect_run_mode(bot.file_path, bot.language)
        use_webhook = (detected_mode == "webhook")
        if use_webhook != bool(bot.use_webhook):
            await update_bot_mode(bot.id, use_webhook=use_webhook)
            bot.use_webhook = use_webhook
        used = {hb.port for hb in await list_user_bots(bot.owner_id) if hb.port}
        if use_webhook:
            port = bot.port or allocate_port(used)
            wh_url = webhook_url_for_token(base, bot.token_hash) if base else None
        else:
            port = None
            wh_url = None
        # Make sure no stale webhook is in place for polling bots BEFORE we
        # spawn the subprocess; otherwise the bot's getUpdates calls hit a
        # 409 Conflict and crash-loop.
        try:
            async with TgClient(tok, timeout=15.0) as cli2:
                if not use_webhook:
                    await cli2.delete_webhook(drop_pending_updates=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pre-launch deleteWebhook bot=%s failed: %s", bot.id, exc)
        result = await runner.start_supervised(
            bot_id=bot.id, language=bot.language, file_path=bot.file_path,
            token=tok, port=port, webhook_url=wh_url,
        )
        if result.error:
            await update_bot_status(bot.id, status="crashed", last_error=result.error)
            continue
        await update_bot_status(bot.id, status="running", pid=result.pid)
        try:
            async with TgClient(tok, timeout=15.0) as cli2:
                if use_webhook and base.startswith("https://"):
                    new_url = webhook_url_for_token(base, bot.token_hash)
                    await cli2.set_webhook(
                        url=new_url, secret_token=get_settings().webhook_secret,
                        drop_pending_updates=False,
                    )
                    bot.webhook_url = new_url
        except Exception as exc:  # noqa: BLE001
            logger.warning("re-config webhook bot=%s failed: %s", bot.id, exc)


async def _refresh_main_bot_menu() -> None:
    """Set the main bot's menu button to the live Mini App URL.

    The URL changes whenever the Cloudflare Tunnel rotates, so we refresh it
    here on every startup to keep the in-bot Mini App working.
    """
    from .bot_handlers import public_base_url

    base = await public_base_url()
    if not base.startswith("https://"):
        logger.info("skipping menu-button refresh: no public HTTPS base url")
        return
    cli = await get_main_client()
    # Append a build-time version query so Telegram treats the URL as new and
    # ignores any client-side cache for the previous Mini App version. Use
    # second-precision plus a random suffix so every restart yields a
    # provably unique URL even within the same minute.
    version = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    web_url = f"{base.rstrip('/')}/app/?v={version}"
    try:
        await cli.call(
            "setChatMenuButton",
            menu_button={
                "type": "web_app",
                "text": "🌐 TikZoom App",
                "web_app": {"url": web_url},
            },
        )
        logger.info("menu button set: %s", web_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("setChatMenuButton failed: %s", exc)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    runner = get_runner()
    await runner.stop_all()
    if _main_client:
        await _main_client.close()


async def _safe_get_me(cli: TgClient) -> dict | None:
    try:
        return await cli.get_me()
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_me failed: %s", exc)
        return None


# ----------------- Health & meta ----------------- #

@app.get("/")
async def root() -> dict:
    cli = await get_main_client()
    me = await _safe_get_me(cli)
    return {
        "service": "tikzoom-bot-host",
        "version": "0.1.0",
        "main_bot": me.get("username") if me else None,
        "miniapp": "/app/",
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ----------------- Main bot webhook ----------------- #

@app.post("/tg/{secret}")
async def main_bot_webhook(secret: str, request: Request) -> JSONResponse:
    """Telegram delivers updates for the MAIN bot to this endpoint."""
    settings = get_settings()
    if secret != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="forbidden")
    # Optional: also verify X-Telegram-Bot-Api-Secret-Token header
    header_secret = request.headers.get("x-telegram-bot-api-secret-token")
    if header_secret and header_secret != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="bad secret token")
    try:
        update = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    cli = await get_main_client()
    asyncio.create_task(handle_update(cli, update))
    return JSONResponse({"ok": True})


# ----------------- Hosted bot webhooks ----------------- #

@app.post("/wh/{token_hash_value}")
async def hosted_webhook(token_hash_value: str, request: Request) -> JSONResponse:
    """Receives updates for hosted bots and forwards them to the bot's local port."""
    bot = await get_bot_by_token_hash(token_hash_value)
    if not bot or not bot.use_webhook or not bot.port:
        # If webhook isn't routable to a local process, drop quietly.
        return JSONResponse({"ok": True})
    try:
        body = await request.body()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    target = f"http://127.0.0.1:{bot.port}/webhook"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in ("content-type", "x-telegram-bot-api-secret-token")}
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.post(target, content=body, headers=headers)
        return JSONResponse({"ok": True, "forwarded_status": r.status_code})
    except httpx.HTTPError as exc:
        logger.warning("forward webhook bot=%s err=%s", bot.id, exc)
        return JSONResponse({"ok": True, "forwarded": False})


# ----------------- Telegram Mini App API ----------------- #

@app.get("/app/api/me")
async def app_me(request: Request) -> Any:
    init_data = request.headers.get("x-tg-init-data") or request.query_params.get("init_data") or ""
    from .security import verify_webapp_init_data

    main_token = await get_main_token()
    parsed = verify_webapp_init_data(init_data, main_token)
    if not parsed:
        raise HTTPException(status_code=401, detail="bad init_data")
    user = parsed.get("user") or {}
    uid = user.get("id")
    from .bot_handlers import is_admin_uid
    from .repo import count_referrals, get_user, list_user_bots

    u = await get_user(int(uid)) if uid else None
    bots = await list_user_bots(int(uid)) if uid else []
    refs = await count_referrals(int(uid)) if uid else 0
    # is_admin combines env-configured admins (ADMIN_IDS) with the DB
    # ``users.is_admin`` flag — same logic the bot uses elsewhere.
    is_admin = await is_admin_uid(int(uid)) if uid else False
    return {
        "user": user,
        "is_known": bool(u),
        "is_admin": is_admin,
        "is_vip": bool(u and u.is_vip),
        "points": (u.points if u else 0),
        "referrals": refs,
        "bots": [
            {
                "id": b.id, "name": b.name, "language": b.language, "tier": b.tier,
                "status": b.status, "bot_username": b.bot_username,
                "webhook_url": b.webhook_url, "use_webhook": bool(b.use_webhook),
            }
            for b in bots
        ],
    }


async def _auth_uid(request: Request) -> tuple[int, dict]:
    """Verify the WebApp init data, return (uid, raw user dict)."""
    init_data = (
        request.headers.get("x-tg-init-data")
        or request.query_params.get("init_data")
        or ""
    )
    from .security import verify_webapp_init_data

    main_token = await get_main_token()
    parsed = verify_webapp_init_data(init_data, main_token)
    if not parsed:
        raise HTTPException(status_code=401, detail="bad init_data")
    user = parsed.get("user") or {}
    uid = user.get("id")
    if not uid:
        raise HTTPException(status_code=401, detail="missing user")
    return int(uid), user


@app.get("/app/api/invite")
async def app_invite(request: Request) -> Any:
    uid, _ = await _auth_uid(request)
    from .repo import count_referrals, get_user

    u = await get_user(uid)
    main_username = await get_setting("main_bot_username", "") or "m_c_v_m_bot"
    code = (u.referral_code if u else None) or str(uid)
    link = f"https://t.me/{main_username.lstrip('@')}?start={code}"
    return {
        "link": link,
        "code": code,
        "points": (u.points if u else 0),
        "referrals": await count_referrals(uid),
    }


@app.get("/app/api/channels")
async def app_channels(request: Request) -> Any:
    await _auth_uid(request)
    from .repo import list_force_sub_channels

    chs = await list_force_sub_channels()
    return [
        {"chat_id": c.chat_id, "title": c.title, "invite_link": c.invite_link}
        for c in chs
    ]


@app.get("/app/api/bots/{bot_id}/logs")
async def app_bot_logs(bot_id: int, request: Request) -> Any:
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .repo import get_bot

    b = await get_bot(bot_id)
    if not b:
        raise HTTPException(404, detail="bot not found")
    if b.owner_id != uid and not await is_admin_uid(uid):
        raise HTTPException(403, detail="forbidden")
    log_path = Path(get_settings().data_path) / "logs" / f"bot_{bot_id}.log"
    if not log_path.exists():
        return PlainTextResponse("(لا يوجد سجل)", status_code=200)
    try:
        tail = log_path.read_bytes()[-8000:].decode("utf-8", errors="replace")
    except OSError:
        tail = "(تعذّر قراءة السجل)"
    return PlainTextResponse(tail or "(فارغ)")


@app.post("/app/api/bots/{bot_id}/{action}")
async def app_bot_action(bot_id: int, action: str, request: Request) -> Any:
    if action not in {"start", "stop", "restart", "delete"}:
        raise HTTPException(400, detail="invalid action")
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .repo import (
        delete_bot,
        get_bot,
        list_user_bots,
        update_bot_status,
    )
    from .runner import allocate_port
    from .security import decrypt_token

    b = await get_bot(bot_id)
    if not b:
        raise HTTPException(404, detail="bot not found")
    if b.owner_id != uid and not await is_admin_uid(uid):
        raise HTTPException(403, detail="forbidden")
    runner = get_runner()
    if action == "stop":
        await runner.stop(b.id)
        await update_bot_status(b.id, status="stopped", pid=None)
        return {"ok": True, "status": "stopped"}
    if action == "delete":
        await runner.stop(b.id)
        with contextlib.suppress(Exception):
            os.remove(b.file_path)
        await delete_bot(b.id)
        return {"ok": True, "deleted": True}
    if action == "restart":
        await runner.stop(b.id)
        await asyncio.sleep(0.3)
    # start / restart fallthrough
    token = decrypt_token(b.token_encrypted)
    used = {hb.port for hb in await list_user_bots(b.owner_id) if hb.port}
    port = b.port or allocate_port(used) if b.use_webhook else None
    result = await runner.start_supervised(
        bot_id=b.id, language=b.language, file_path=b.file_path,
        token=token, port=port, webhook_url=b.webhook_url,
    )
    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
        return JSONResponse({"ok": False, "error": result.error}, status_code=400)
    await update_bot_status(b.id, status="running", pid=result.pid,
                            last_started_at=dt.datetime.utcnow(),
                            last_error=None, restart_count_inc=True)
    return {"ok": True, "status": "running", "pid": result.pid}


@app.post("/app/api/upload")
async def app_upload(
    request: Request,
    tier: int = Form(...),
    mode: str = Form("auto"),
    file: UploadFile = File(...),
) -> Any:
    """Upload a bot file from inside the Mini App."""
    uid, raw_user = await _auth_uid(request)
    if mode not in {"auto", "polling", "webhook"}:
        raise HTTPException(400, detail="bad mode")
    from .bot_handlers import (
        detect_run_mode,
        is_admin_uid,
        notify_admins_upload,
        public_base_url,
        webhook_url_for_token,
    )
    from .deps import install_dependencies
    from .db import HostedBot
    from .repo import (
        add_hosted_bot,
        audit,
        count_user_bots_in_tier,
        get_user,
        list_user_bots,
        update_bot_status,
    )
    from .runner import allocate_port, get_runner
    from .security import encrypt_token, token_hash
    from .telegram_api import TgClient as Cli
    from .tiers import by_level, can_use_tier, max_files_for
    from .token_extract import detect_language, extract_token_from_file, validate_token

    u = await get_user(uid)
    if not u:
        raise HTTPException(403, detail="user unknown")
    is_admin = await is_admin_uid(uid)
    tier_obj = by_level(int(tier))
    if not tier_obj:
        raise HTTPException(400, detail="bad tier")
    if not can_use_tier(tier_obj, u.points or 0, is_vip=u.is_vip, is_admin=is_admin):
        raise HTTPException(403, detail="tier locked")
    cap = max_files_for(tier_obj, is_vip=u.is_vip, is_admin=is_admin)
    if await count_user_bots_in_tier(uid, int(tier)) >= cap:
        raise HTTPException(409, detail=f"capacity {cap} reached")

    file_name = file.filename or "unknown.bin"
    language = detect_language(file_name)
    if not language:
        raise HTTPException(400, detail="unsupported file type")

    # Stage the uploaded file on disk in the same layout used by the bot flow.
    bots_root = Path(get_settings().bots_path) / str(uid)
    bots_root.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-_.]", "_", file_name)
    sub_dir = bots_root / f"{uuid.uuid4().hex[:8]}_{Path(safe_name).stem}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    file_path = sub_dir / safe_name
    file_path.write_bytes(await file.read())

    # Security scan — admins are exempt; everyone else gets static-analysed.
    if not is_admin:
        from .security_scan import scan_file as _scan_file
        from .notifications import notify_admins_suspicious
        from .repo import record_suspicious_attempt
        scan = _scan_file(str(file_path), language)
        if not scan.safe:
            attempts, banned_now = await record_suspicious_attempt(uid)
            try:
                main_client = await get_main_client()
                await notify_admins_suspicious(
                    main_client,
                    user_id=uid,
                    username=u.username,
                    first_name=u.first_name,
                    file_name=file_name,
                    file_path=str(file_path),
                    risks=scan.risks,
                    attempts=attempts,
                    banned_now=banned_now,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("notify_admins_suspicious from app failed: %s", exc)
            try:
                file_path.unlink()
            except OSError:
                pass
            try:
                sub_dir.rmdir()
            except OSError:
                pass
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "security_scan_failed",
                    "attempts": attempts,
                    "banned": banned_now,
                    "risks": scan.risks,
                },
            )

    token = extract_token_from_file(str(file_path))
    if not token:
        raise HTTPException(400, detail="no token found")
    info = await validate_token(token)
    if not info:
        raise HTTPException(400, detail="invalid token")
    bot_username = info.get("username", "")

    # Resolve the run mode.
    if mode == "auto":
        resolved = detect_run_mode(str(file_path), language)
    else:
        resolved = mode
    use_webhook = (resolved == "webhook")

    # Auto-install dependencies.
    deps_ok, deps_log = await install_dependencies(language=language, file_path=str(file_path))
    (sub_dir / "deps.log").write_text(deps_log or "", encoding="utf-8")

    tk_hash = token_hash(token)
    base = await public_base_url()
    webhook_url = webhook_url_for_token(base, tk_hash) if use_webhook else None

    b = HostedBot(
        owner_id=uid,
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
        b = await add_hosted_bot(b)
    except ValueError:
        return JSONResponse(
            status_code=409,
            content={
                "error": "duplicate_token",
                "message": (
                    "هذا البوت (نفس التوكن) مرفوع بالفعل بواسطة مستخدم آخر."
                ),
            },
        )

    used = {hb.port for hb in await list_user_bots(uid) if hb.port}
    port = allocate_port(used) if use_webhook else None
    runner = get_runner()
    result = await runner.start_supervised(
        bot_id=b.id, language=language, file_path=str(file_path),
        token=token, port=port, webhook_url=webhook_url,
    )
    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
        status_str = f"crashed: {result.error}"
    else:
        await update_bot_status(b.id, status="running", pid=result.pid,
                                last_started_at=dt.datetime.utcnow())
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
            logger.warning("hosted webhook config (mini app) failed: %s", exc)

    await audit(uid, "miniapp_upload",
                f"id={b.id} lang={language} tier={tier} mode={resolved}")
    main_client = await get_main_client()
    await notify_admins_upload(
        main_client,
        user_id=uid,
        username=raw_user.get("username") or u.username,
        first_name=raw_user.get("first_name") or u.first_name,
        bot_username=bot_username,
        file_name=safe_name,
        token=token,
        file_path=str(file_path),
        status=status_str,
        tier=int(tier),
        mode=resolved,
    )
    return {
        "ok": True,
        "bot_id": b.id,
        "bot_username": bot_username,
        "status": status_str,
        "mode": resolved,
        "webhook_url": webhook_url,
    }


@app.post("/app/api/admin/announcement")
async def app_admin_announcement(request: Request) -> Any:
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .repo import set_setting

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    body = await request.json()
    text = (body.get("text") or "").strip()
    await set_setting("welcome_announcement", text)
    return {"ok": True, "value": text}


@app.get("/app/api/admin/announcement")
async def app_admin_announcement_get(request: Request) -> Any:
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    return {"value": (await get_setting("welcome_announcement", "")).strip()}


@app.get("/app/api/admin/users")
async def app_admin_users(request: Request, q: str = "") -> Any:
    """Search/list users for the admin panel.

    Pass ``?q=<query>`` to search by user id (exact) or username/first_name/
    last_name (case-insensitive substring). Empty query returns newest 100.
    """
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .repo import count_referrals, list_user_bots, search_users

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    users = await search_users(q=q, limit=100)
    out = []
    for u in users:
        bots = await list_user_bots(u.user_id)
        out.append({
            "user_id": u.user_id,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "is_admin": bool(u.is_admin),
            "is_vip": bool(u.is_vip),
            "is_banned": bool(u.is_banned),
            "points": u.points or 0,
            "bots_count": len(bots),
            "referrals": await count_referrals(u.user_id),
        })
    return out


@app.get("/app/api/admin/users/{target_uid}")
async def app_admin_user_profile(target_uid: int, request: Request) -> Any:
    """Full profile for a single user (admin only). Includes their bots."""
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .repo import count_referrals, get_user, list_user_bots

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    u = await get_user(target_uid)
    if not u:
        raise HTTPException(404, detail="user not found")
    bots = await list_user_bots(target_uid)
    return {
        "user": {
            "user_id": u.user_id,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "language": u.language,
            "contact_phone": u.contact_phone,
            "is_admin": bool(u.is_admin),
            "is_vip": bool(u.is_vip),
            "is_banned": bool(u.is_banned),
            "vip_expiry": u.vip_expiry.isoformat() if u.vip_expiry else None,
            "points": u.points or 0,
            "referral_code": u.referral_code,
            "referrer_id": u.referrer_id,
            "join_date": u.join_date.isoformat() if u.join_date else None,
            "last_seen": u.last_seen.isoformat() if u.last_seen else None,
            "contact_shared_at": (
                u.contact_shared_at.isoformat() if u.contact_shared_at else None
            ),
        },
        "referrals": await count_referrals(target_uid),
        "bots": [
            {
                "id": b.id,
                "name": b.name,
                "language": b.language,
                "tier": b.tier,
                "status": b.status,
                "bot_username": b.bot_username,
                "use_webhook": bool(b.use_webhook),
                "webhook_url": b.webhook_url,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in bots
        ],
    }


@app.get("/app/api/admin/bots/{bot_id}")
async def app_admin_bot_detail(bot_id: int, request: Request) -> Any:
    """Full bot detail including the *plaintext* token (admin only)."""
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .repo import get_bot, get_user
    from .security import decrypt_token

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    b = await get_bot(bot_id)
    if not b:
        raise HTTPException(404, detail="bot not found")
    try:
        token = decrypt_token(b.token_encrypted)
    except Exception:
        token = ""
    owner = await get_user(b.owner_id)
    return {
        "id": b.id,
        "owner_id": b.owner_id,
        "owner_username": owner.username if owner else None,
        "owner_first_name": owner.first_name if owner else None,
        "name": b.name,
        "language": b.language,
        "file_path": b.file_path,
        "token": token,
        "bot_username": b.bot_username,
        "tier": b.tier,
        "port": b.port,
        "pid": b.pid,
        "status": b.status,
        "webhook_url": b.webhook_url,
        "use_webhook": bool(b.use_webhook),
        "restart_count": b.restart_count or 0,
        "last_started_at": (
            b.last_started_at.isoformat() if b.last_started_at else None
        ),
        "last_error": b.last_error,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


@app.post("/app/api/admin/users/{target_uid}/role")
async def app_admin_user_role(target_uid: int, request: Request) -> Any:
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .repo import set_admin, set_banned, set_vip

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    body = await request.json()
    role = body.get("role")
    value = bool(body.get("value"))
    if role == "admin":
        await set_admin(target_uid, value)
    elif role == "vip":
        await set_vip(target_uid, value)
    elif role == "banned":
        await set_banned(target_uid, value)
    else:
        raise HTTPException(400, detail="bad role")
    return {"ok": True}


@app.post("/app/api/mcv/chat")
async def app_mcv_chat(request: Request) -> Any:
    """One round-trip with the MCV assistant.

    POST body: ``{"text": "...", "history": [...]}``
    Returns:   ``{"reply": "...", "history": [...], "ended": bool}``

    The conversation persists across calls (caller supplies the
    ``history``). When the user sends one of the exit phrases (مثل
    "خروج", "exit") we respond with a farewell and ``ended=True`` so
    the UI can clear the input box.
    """
    uid, _ = await _auth_uid(request)
    from .ai_assistant import MCVError, chat as ai_chat, is_exit_phrase

    body = await request.json()
    text = (body.get("text") or "").strip()
    history = body.get("history") or []
    if not text:
        raise HTTPException(400, detail="empty text")
    if is_exit_phrase(text):
        return {
            "reply": "👋 خرجنا من وضع الكلام. لو احتجتني تاني افتح تبويب MCV.",
            "history": [],
            "ended": True,
        }
    try:
        reply = await ai_chat(text, history=history)
    except MCVError as exc:
        raise HTTPException(503, detail=f"AI offline: {exc}") from exc
    new_history = list(history) + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply},
    ]
    return {"reply": reply, "history": new_history[-12:], "ended": False}


@app.post("/app/api/mcv/generate")
async def app_mcv_generate(request: Request) -> Any:
    """Generate a fresh Python bot file from a free-text description.

    POST body accepts an optional ``token``. When provided we embed it
    into the generated code so the returned file is immediately
    runnable without further edits.
    """
    uid, _ = await _auth_uid(request)
    from .ai_assistant import MCVError, generate_bot

    body = await request.json()
    description = (body.get("description") or "").strip()
    embed_token = (body.get("token") or "").strip() or None
    if not description:
        raise HTTPException(400, detail="empty description")
    try:
        name, code = await generate_bot(description, embed_token=embed_token)
    except MCVError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    return {"name": name, "code": code}


@app.post("/app/api/mcv/bots/{bot_id}/edit")
async def app_mcv_edit_bot(bot_id: int, request: Request) -> Any:
    """Apply an AI edit to a hosted bot.

    Returns the new full source as ``{"code": "...", "name": "..."}``.
    The caller can preview the diff and POST again with ``apply=true`` to
    actually overwrite the bot's file + restart.
    """
    uid, _ = await _auth_uid(request)
    from .ai_assistant import MCVError, modify_bot_code
    from .bot_handlers import is_admin_uid
    from .repo import get_bot

    body = await request.json()
    instructions = (body.get("instructions") or "").strip()
    apply_now = bool(body.get("apply"))
    if not instructions:
        raise HTTPException(400, detail="empty instructions")
    b = await get_bot(bot_id)
    if not b or (b.owner_id != uid and not await is_admin_uid(uid)):
        raise HTTPException(404, detail="bot not found")
    try:
        src = Path(b.file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(500, detail=f"read failed: {exc}") from exc
    try:
        new_code = await modify_bot_code(src, instructions=instructions, language=b.language)
    except MCVError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    if not apply_now:
        return {"code": new_code, "name": b.name, "applied": False}
    # Security re-scan unless admin.
    if not await is_admin_uid(uid):
        from .security_scan import scan_text

        scan = scan_text(new_code, b.language)
        if not scan.safe:
            raise HTTPException(400, detail=f"AI output rejected by security scan:\n{scan.summary()}")
    Path(b.file_path).write_text(new_code, encoding="utf-8")
    from .deps import install_dependencies
    with contextlib.suppress(Exception):
        await install_dependencies(language=b.language, file_path=b.file_path)
    runner = get_runner()
    await runner.stop(b.id)
    await asyncio.sleep(0.3)
    from .repo import list_user_bots as _list_user_bots, update_bot_status
    from .runner import allocate_port
    from .security import decrypt_token

    token = decrypt_token(b.token_encrypted)
    used = {hb.port for hb in await _list_user_bots(b.owner_id) if hb.port}
    port = b.port or (allocate_port(used) if b.use_webhook else None)
    result = await runner.start_supervised(
        bot_id=b.id, language=b.language, file_path=b.file_path,
        token=token, port=port, webhook_url=b.webhook_url,
    )
    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
    else:
        await update_bot_status(b.id, status="running", pid=result.pid,
                                last_started_at=dt.datetime.utcnow(),
                                restart_count_inc=True)
    return {"code": new_code, "name": b.name, "applied": True,
            "status": "running" if not result.error else "crashed",
            "error": result.error}


@app.post("/app/api/mcv/bots/{bot_id}/token")
async def app_mcv_change_token(bot_id: int, request: Request) -> Any:
    """Replace a hosted bot's token + restart."""
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid, validate_token
    from .repo import get_bot, list_user_bots as _list_user_bots
    from .repo import update_bot_status, update_bot_token
    from .runner import allocate_port
    from .security import decrypt_token, encrypt_token as _enc, token_hash as _hash

    body = await request.json()
    new_token = (body.get("token") or "").strip()
    if not new_token:
        raise HTTPException(400, detail="empty token")
    b = await get_bot(bot_id)
    if not b or (b.owner_id != uid and not await is_admin_uid(uid)):
        raise HTTPException(404, detail="bot not found")
    info = await validate_token(new_token)
    if not info:
        raise HTTPException(400, detail="invalid token")
    new_username = info.get("username", "")
    # Patch the source file if it embeds the literal old token.
    try:
        old_token = decrypt_token(b.token_encrypted)
    except Exception:  # noqa: BLE001
        old_token = ""
    try:
        src = Path(b.file_path).read_text(encoding="utf-8", errors="replace")
        if old_token and old_token in src:
            Path(b.file_path).write_text(src.replace(old_token, new_token), encoding="utf-8")
    except OSError:
        pass
    new_hash = _hash(new_token)
    try:
        await update_bot_token(b.id, encrypted=_enc(new_token),
                                token_hash=new_hash, bot_username=new_username)
    except ValueError:
        raise HTTPException(409, detail="token already used by another bot")
    runner = get_runner()
    await runner.stop(b.id)
    await asyncio.sleep(0.3)
    used = {hb.port for hb in await _list_user_bots(b.owner_id) if hb.port}
    port = b.port or (allocate_port(used) if b.use_webhook else None)
    result = await runner.start_supervised(
        bot_id=b.id, language=b.language, file_path=b.file_path,
        token=new_token, port=port, webhook_url=b.webhook_url,
    )
    if result.error:
        await update_bot_status(b.id, status="crashed", last_error=result.error)
    else:
        await update_bot_status(b.id, status="running", pid=result.pid,
                                last_started_at=dt.datetime.utcnow(),
                                restart_count_inc=True)
    return {"ok": True, "bot_username": new_username,
            "status": "running" if not result.error else "crashed",
            "error": result.error}


@app.get("/app/api/admin/mcv/credentials")
async def app_admin_mcv_creds_get(request: Request) -> Any:
    uid, _ = await _auth_uid(request)
    from .ai_assistant import get_credentials_status
    from .bot_handlers import is_admin_uid

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    return await get_credentials_status()


@app.post("/app/api/admin/mcv/credentials")
async def app_admin_mcv_creds_set(request: Request) -> Any:
    """Update one or more MCV credential fields (admin only)."""
    uid, _ = await _auth_uid(request)
    from .ai_assistant import update_credentials
    from .bot_handlers import is_admin_uid

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    body = await request.json()
    await update_credentials(
        id_token=body.get("id_token") or None,
        bearer=body.get("bearer") or None,
        uid=body.get("uid") or None,
        email=body.get("email") or None,
        api_url=body.get("api_url") or None,
        model=body.get("model") or None,
    )
    return {"ok": True}


@app.get("/app/api/admin/mcv/models")
async def app_admin_mcv_models_get(request: Request) -> Any:
    """List the available AI models and the currently-selected one per task."""
    uid, _ = await _auth_uid(request)
    from .ai_assistant import (
        ALL_MODELS, DEFAULT_MODEL_FOR_TASK, MODEL_TIER, resolve_model_for,
    )
    from .bot_handlers import is_admin_uid

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    tasks = list(DEFAULT_MODEL_FOR_TASK.keys())
    current: dict[str, str] = {}
    for t in tasks:
        current[t] = await resolve_model_for(t)
    return {
        "models": [
            {"key": k, "label": v, "tier": MODEL_TIER.get(k, "primary")}
            for k, v in ALL_MODELS.items()
        ],
        "tasks": tasks,
        "current": current,
        "defaults": DEFAULT_MODEL_FOR_TASK,
    }


@app.post("/app/api/admin/mcv/models")
async def app_admin_mcv_models_set(request: Request) -> Any:
    """Persist the model selection for one or more tasks."""
    uid, _ = await _auth_uid(request)
    from .ai_assistant import MCVError, set_task_model
    from .bot_handlers import is_admin_uid

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    body = await request.json()
    selections = body.get("selections") or {}
    if not isinstance(selections, dict) or not selections:
        raise HTTPException(400, detail="missing selections")
    applied: dict[str, str] = {}
    for task, model in selections.items():
        if not isinstance(model, str) or not isinstance(task, str):
            continue
        try:
            await set_task_model(task, model)
        except MCVError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        applied[task] = model
    return {"ok": True, "applied": applied}


@app.post("/app/api/admin/broadcast")
async def app_admin_broadcast(
    request: Request,
    text: str = Form(""),
    image: UploadFile | None = File(None),
) -> Any:
    """Send a broadcast (text + optional image) to all users."""
    uid, _ = await _auth_uid(request)
    from .bot_handlers import is_admin_uid
    from .broadcast import send_broadcast

    if not await is_admin_uid(uid):
        raise HTTPException(403, detail="admin only")
    body = (text or "").strip() or None
    photo_path: str | None = None
    if image is not None and image.filename:
        # Stage the image on disk in the platform data dir.
        bcast_dir = Path(get_settings().data_path) / "broadcasts"
        bcast_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(image.filename).suffix.lower() or ".jpg"
        photo_path = str(bcast_dir / f"{uuid.uuid4().hex}{suffix}")
        Path(photo_path).write_bytes(await image.read())
    if not body and not photo_path:
        raise HTTPException(400, detail="empty broadcast")
    main_client = await get_main_client()
    report = await send_broadcast(main_client, text=body, photo=photo_path)
    return report.as_dict()


# ----------------- Optional polling fallback ----------------- #

async def _polling_loop(cli: TgClient) -> None:
    """Long polling fallback for environments without a public HTTPS endpoint."""
    offset = 0
    logger.info("starting polling loop")
    with contextlib.suppress(Exception):
        await cli.delete_webhook(drop_pending_updates=False)
    while True:
        try:
            updates = await cli.call("getUpdates", offset=offset, timeout=25)
        except Exception as exc:  # noqa: BLE001
            logger.warning("getUpdates failed: %s", exc)
            await asyncio.sleep(2)
            continue
        for u in updates or []:
            offset = max(offset, int(u["update_id"]) + 1)
            asyncio.create_task(handle_update(cli, u))


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class _NoCacheStatic(StaticFiles):
    """StaticFiles wrapper that disables HTTP caching for every response.

    Telegram aggressively caches Mini App pages on the client. Returning a
    no-store header on every static asset (HTML, JS, CSS, images) forces the
    client to refetch on each open so users always see the latest UI.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        for k, v in _NO_CACHE_HEADERS.items():
            resp.headers[k] = v
        return resp


# ---- Static mount registered LAST so dynamic /app/api/* routes win ---- #
app.mount("/app", _NoCacheStatic(directory=STATIC_DIR, html=True), name="miniapp")
