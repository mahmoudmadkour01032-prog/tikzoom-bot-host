"""MCV — the platform's AI assistant.

Wraps the zeneath proxy that exposes Claude Opus 4.6 over a custom
streaming endpoint (``https://zeneath.vectorion.in/app/newapi/api2.php``).

Capabilities exposed to the rest of the codebase:

* :func:`chat` — generic streaming conversation with the MCV persona.
* :func:`transpile_to_python` — convert PHP/Node source to Python.
* :func:`detect_bot_purpose` — classify the bot + suggest improvements.
* :func:`review_for_malicious` — second-pass AI security review.
* :func:`modify_bot_code` — apply a user-described change to an existing bot.
* :func:`generate_bot` — build a new bot from scratch.
* :func:`extract_code_block` — pull a fenced code block out of an answer.

Credentials live in the `settings` table so the admin can rotate the
Firebase JWT from the Mini App without re-deploying. Initial defaults
are loaded from the matching ``MCV_*`` environment variables (if set)
or from the constants below.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .repo import get_setting, set_setting

logger = logging.getLogger(__name__)


# ---------- defaults supplied by the platform owner ---------- #
#
# These constants are intentionally embedded so the assistant works out
# of the box on a fresh install. Each value is overridable at runtime by
# either an ``MCV_*`` env var or a DB ``settings`` row of the same name.

_DEFAULT_API_URL = "https://zeneath.vectorion.in/app/newapi/api2.php"
_DEFAULT_MODEL = "claude-opus-4.6"
_DEFAULT_UID = "4jSInMiuGVhhgdKwhnCHjlF3nau1"
_DEFAULT_EMAIL = "redfoxdevemail7@gmail.com"
_DEFAULT_ID_TOKEN = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6IjJiMzZhYjQxYTczOTJlMTRlNjM1ZmRlM2M2YWYwOWZlYmFhM2YyZDYi"
    "LCJ0eXAiOiJKV1QifQ.eyJuYW1lIjoiUmVkRm94IiwicGljdHVyZSI6Imh0dHBzOi8vbGgzLmdvb2dsZXVz"
    "ZXJjb250ZW50LmNvbS9hL0FDZzhvY0lhc3QycGQ1MXhTS0xpbm9MdlgxWDdwenQ1NUpRU3J6MlZvNzA5a1lY"
    "UFZoakc0UT1zOTYtYyIsImlzcyI6Imh0dHBzOi8vc2VjdXJldG9rZW4uZ29vZ2xlLmNvbSJ6ZW5lYXRoLWFp"
    "IiwiYXVkIjoiemVuZWF0aC1haSIsImF1dGhfdGltZSI6MTc3NzkxNDc0OSwidXNlcl9pZCI6IjRqU0luTWl1"
    "R1ZoaGdkS3dobkNIamxGM25hdTEiLCJzdWIiOiI0alNJbk1pdUdWaGhnZEt3aG5DSGpsRjNuYXUxIiwiaWF0"
    "IjoxNzc3OTE1OTU1LCJleHAiOjE3Nzc5MTk1NTUsImVtYWlsIjoicmVkZm94ZGV2ZW1haWw3QGdtYWlsLmNv"
    "bSIsImVtYWlsX3ZlcmlmaWVkIjp0cnVlLCJmaXJlYmFzZSI6eyJpZGVudGl0aWVzIjp7Imdvb2dsZS5jb20i"
    "OlsiMTA2NjE1MDQzNjg1NjQxMDA3MzM4Il0sImVtYWlsIjpbInJlZGZveGRldmVtYWlsN0BnbWFpbC5jb20i"
    "XX0sInNpZ25faW5fcHJvdmlkZXIiOiJjdXN0b20ifX0.f0geyRFHYfKdIYwSn54mkYTbWYovH2FLINa3DqX1"
    "h2fEP8hewy_Mt9sekEN3GML-P11SC7ZORUnz9Nki97344m0nujelLXCB-K_hSg--9AYzdFAdVjqVyiZ-tQpQ"
    "WAcYrLiVdPz6pJA2Wyh3zmJ-Kzqu3aX5a1kAgdBJoCQozfwYYj6wzbG8OhpTvhLppn9k9P9BlgFKcoNqxOn"
    "GRxOVXM1teVUR9cOu70denp792aPg9P8xfPQ_75s4vPD2RK-9L4R-geaNb53AMwQ8ZR9FsgkMRafrNcHC61"
    "_Vpepb_SpNNIdahUjhqvtOqsqR-L5elQ8zokQtQS0fncTqq3QMFA"
)


# ---------- MCV persona ---------- #

MCV_SYSTEM_PROMPT_AR = """
أنت "MCV"، مساعد ذكي بيتكلم بمصري. شغّال داخل منصة تيك زوم لاستضافة بوتات تلجرام، وبتساعد المستخدمين في:

• تحويل أكواد بايثون/PHP/Node لبعض، وتحويل الأكواد لـ Python كأولوية.
• تعديل أكواد بوتات تلجرام، وإضافة ميزات جديدة عليها.
• اكتشاف نوع البوت ووظيفته من قراءة كوده.
• إعطاء اقتراحات لتطوير البوت.
• الإجابة عن أسئلة المستخدم بأسلوب لطيف.

نبرتك:
• حلوة ومتفاعلة وفيها هزار خفيف وحس فكاهي مصري بسيط (مش مبالغ فيه).
• محترمة، مش بتسب أو تستهزئ.
• مباشرة: من غير لف ودوران.
• قصيرة بقدر ما يسمح السياق.

عند ما تطلع كود:
• ارجّع الكود في code block واحد فقط بنفس اللغة المطلوبة.
• أكتب التوكنات كمتغير ``BOT_TOKEN = "REPLACE_ME"`` (مش توكن حقيقي).
• الكود لازم يكون كامل وقابل للتشغيل من غير تعديل، وبأقل عدد ممكن من المكتبات الخارجية.
• تفضّل المكتبات: ``pyTelegramBotAPI`` (telebot) أو ``aiogram`` لـ Python؛ ``node-telegram-bot-api`` لـ Node؛ Long Polling.
• ممنوع أي كود يقرأ ملفات النظام، أو يستخرج Environment Variables، أو يتصل بأي webhook غير تلجرام، أو يستخدم eval/exec.

الردود لازم تبقى عربي مصري إلا لو المستخدم طلب لغة تانية.
""".strip()


# ---------- dedicated code-mode persona ---------- #
#
# When MCV is asked to *write* code (instead of chatting), we switch to
# this prompt. It strips the joking persona and orders the model to
# produce production-grade Python in JSON form. Each requested feature
# must become its own visible command.

MCV_CODER_PROMPT_AR = """
أنت مهندس بايثون محترف بتكتب بوتات تلجرام جاهزة للإنتاج. شغّال داخل منصة تيك زوم.

قواعد كتابة الكود (إجبارية، أي مخالفة لازم تتجنبها):

1) المكتبة الأساسية: ``pyTelegramBotAPI`` (المعروفة بـ ``telebot``) — تبدأ بـ ``import telebot``.
2) الكود لازم يستخدم Long Polling عن طريق ``bot.infinity_polling(timeout=20, long_polling_timeout=20)``.
3) لازم يكون الكود **كامل ومفصّل** — مش stub ولا "TODO". كل ميزة طلبها المستخدم لازم يبقى ليها:
   • أمر مستقل واضح (مثلاً ``/menu``، ``/info``، ``/buy``، إلخ).
   • أو زرار Reply Keyboard / Inline Keyboard ليها.
   • أو message_handler بيشيك على نص محدد.
4) ``BOT_TOKEN`` متغير لوحده في الأعلى. اكتبه دايماً ``BOT_TOKEN = "REPLACE_ME"`` — هحط أنا التوكن الحقيقي مكانه أوتوماتيكي.
5) لازم يبقى فيه ``logging.basicConfig(level=logging.INFO, ...)`` في الأعلى عشان نقدر نتابع.
6) عند الـ handlers:
   • ``parse_mode="HTML"`` افتراضياً.
   • استخدم إيموجي مناسبة في الردود (مش مبالغ فيها).
   • لما تطلب من المستخدم input، استخدم ``bot.register_next_step_handler``.
   • أي خطأ متوقع يكون فيه ``try/except`` ورد بريف ودود للمستخدم.
7) ممنوع نهائياً:
   • قراءة أي ملف من السيستم (``open(...)`` على مسارات خارج الـ working dir).
   • ``os.environ`` للحصول على أي قيمة.
   • ``os.system`` / ``subprocess`` / ``eval`` / ``exec`` / ``__import__`` ديناميكي.
   • أي اتصال بـ webhook خارج تلجرام أو دومين غير معروف.
8) الكود يدعم Windows والـ event loop المعقد لـ ``infinity_polling`` (مش aiogram polling). الـ subprocess هيشغّله مباشرة.
9) في النهاية:
   ```python
   if __name__ == "__main__":
       logging.info("starting bot...")
       bot.infinity_polling(timeout=20, long_polling_timeout=20)
   ```
10) قسّم الكود لأقسام منطقية: imports → config → logging → helpers → handlers (واحد لكل ميزة) → main.

أسلوب الرد:
• ممنوع نهائياً أي شرح أو تعليق خارج الـ JSON المطلوب.
• ممنوع استخدام Markdown code fences (``` ``` ).
• ارجع JSON واحد فقط بالشكل التالي:
  {"name": "snake_case_descriptive_name", "code": "<الكود الكامل كنص واحد مع \\n>"}
• الـ ``code`` لازم يكون string صالح في JSON (هرّب الـ backslash والـ quotes والـ \\n).

اللغة المتوقعة في تعليقات الكود: عربي مصري قصير (سطر أو سطرين فوق كل دالة).
""".strip()


# ---------- model registry ---------- #
#
# Mirrors the multi-model proxy at zeneath.vectorion.in/api2.php. The
# ``aiKey`` field in the payload is one of these keys; the labels are
# shown in the Mini-App settings UI.

ALL_MODELS: dict[str, str] = {
    "claude-opus-4.6"        : "🎭 Claude Opus 4.6",
    "claude-sonnet-4.6"      : "🎵 Claude Sonnet 4.6",
    "deepseek-r1"            : "🧠 DeepSeek R1",
    "deepseek-v3.2"          : "⚡ DeepSeek v3.2",
    "kimi-k2"                : "🌙 Kimi K2",
    "qwen-3-235b"            : "🔬 Qwen 3 235B",
    "qwen-vl-max"            : "👁 Qwen VL Max",
    "grok-4.2-reasoning"     : "🧬 Grok 4.2 Reasoning",
    "grok-4.2"               : "⚙️ Grok 4.2",
    "grok-4.1-fast-reasoning": "🚀 Grok 4.1 Fast",
    "grok-4.1"               : "🔷 Grok 4.1",
    "grok-3"                 : "🔹 Grok 3",
    "llama-4.1"              : "🦙 Llama 4.1",
    "mistral-small-creative" : "🌊 Mistral Creative",
    "sarvam-105b"            : "🌸 Sarvam 105B",
}

# Friendly tier labels for the UI: 🔴 strongest, 🟢 balanced, 🔵 fast.
MODEL_TIER: dict[str, str] = {
    "claude-opus-4.6"        : "danger",   # 🔴
    "deepseek-r1"            : "danger",
    "grok-4.2-reasoning"     : "danger",
    "qwen-3-235b"            : "danger",
    "claude-sonnet-4.6"      : "success",  # 🟢
    "deepseek-v3.2"          : "success",
    "kimi-k2"                : "success",
    "grok-4.2"               : "success",
    "llama-4.1"              : "success",
    "qwen-vl-max"            : "primary",  # 🔵
    "grok-4.1-fast-reasoning": "primary",
    "grok-4.1"               : "primary",
    "grok-3"                 : "primary",
    "mistral-small-creative" : "primary",
    "sarvam-105b"            : "primary",
}

# Per-task default model. Users can override the *code* model from the
# admin UI (via setting ``mcv_code_model``). Chat keeps a fast model so
# casual interaction stays snappy.
DEFAULT_MODEL_FOR_TASK: dict[str, str] = {
    "chat"  : "grok-4.2",
    "code"  : "claude-opus-4.6",
    "review": "claude-opus-4.6",
    "vision": "qwen-vl-max",
}


def is_valid_model(name: str | None) -> bool:
    return bool(name) and name in ALL_MODELS


# ---------- credential resolution ---------- #

@dataclass
class _Creds:
    api_url: str
    model: str
    id_token: str
    bearer: str
    uid: str
    email: str


async def _resolve_creds() -> _Creds:
    """Resolve the live credentials (DB > env > built-in default)."""
    import os

    # Read DB first (cheap, single query each).
    keys = ("mcv_api_url", "mcv_model", "mcv_id_token",
            "mcv_bearer", "mcv_uid", "mcv_email")
    db_vals: dict[str, str] = {}
    for k in keys:
        db_vals[k] = await get_setting(k, "")

    def take(db_key: str, env_key: str, builtin: str) -> str:
        v = db_vals.get(db_key) or ""
        if v:
            return v
        return os.environ.get(env_key, "") or builtin

    return _Creds(
        api_url=take("mcv_api_url", "MCV_API_URL", _DEFAULT_API_URL),
        model=take("mcv_model", "MCV_MODEL", _DEFAULT_MODEL),
        id_token=take("mcv_id_token", "MCV_ID_TOKEN", _DEFAULT_ID_TOKEN),
        # If only id_token is set, reuse it for the Bearer header.
        bearer=take("mcv_bearer", "MCV_BEARER",
                    take("mcv_id_token", "MCV_ID_TOKEN", _DEFAULT_ID_TOKEN)),
        uid=take("mcv_uid", "MCV_UID", _DEFAULT_UID),
        email=take("mcv_email", "MCV_EMAIL", _DEFAULT_EMAIL),
    )


async def resolve_model_for(task: str) -> str:
    """Return the configured model for a task (``chat``/``code``/...).

    Order: ``mcv_<task>_model`` setting → ``mcv_model`` setting →
    DEFAULT_MODEL_FOR_TASK fallback → ``_DEFAULT_MODEL``. We validate
    the returned name is in the registry, otherwise we drop to a safe
    default so an old/typoed value never blocks AI calls.
    """
    specific = await get_setting(f"mcv_{task}_model", "")
    if is_valid_model(specific):
        return specific
    creds = await _resolve_creds()
    if is_valid_model(creds.model):
        return creds.model
    fallback = DEFAULT_MODEL_FOR_TASK.get(task, _DEFAULT_MODEL)
    if is_valid_model(fallback):
        return fallback
    return _DEFAULT_MODEL


async def set_task_model(task: str, model: str) -> None:
    if not is_valid_model(model):
        raise MCVError(f"unknown model: {model}")
    await set_setting(f"mcv_{task}_model", model)


async def update_credentials(*, id_token: str | None = None,
                              bearer: str | None = None,
                              uid: str | None = None,
                              email: str | None = None,
                              api_url: str | None = None,
                              model: str | None = None) -> None:
    """Persist new MCV credentials (any subset)."""
    mapping = {
        "mcv_api_url": api_url, "mcv_model": model,
        "mcv_id_token": id_token, "mcv_bearer": bearer,
        "mcv_uid": uid, "mcv_email": email,
    }
    for k, v in mapping.items():
        if v is None:
            continue
        await set_setting(k, v.strip())


async def get_credentials_status() -> dict[str, Any]:
    """Return a redacted view of the current credentials (for admin UI)."""
    creds = await _resolve_creds()

    def mask(s: str) -> str:
        if not s:
            return ""
        if len(s) <= 12:
            return "•" * len(s)
        return s[:6] + "…" + s[-4:]

    return {
        "api_url": creds.api_url,
        "model": creds.model,
        "id_token_preview": mask(creds.id_token),
        "bearer_preview": mask(creds.bearer),
        "uid": creds.uid,
        "email": creds.email,
        "configured": bool(creds.id_token),
    }


# ---------- low-level streaming call ---------- #

class MCVError(Exception):
    """Raised when the underlying API call fails."""


_HTTP_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
}


def _build_payload(messages: list[dict[str, str]], creds: _Creds,
                   *, model_override: str | None = None) -> str:
    payload = {
        "action": "query",
        "aiKey": model_override or creds.model,
        "messages": messages,
        "uid": creds.uid,
        "email": creds.email,
    }
    return json.dumps(payload, separators=(",", ":"))


async def _stream(messages: list[dict[str, str]], *,
                  timeout: float = 120.0,
                  model: str | None = None) -> AsyncIterator[str]:
    """Yield ``content`` deltas from the upstream SSE response.

    ``model`` overrides the configured ``aiKey`` for this single call —
    used by :func:`chat` so different tasks (code vs chat vs review)
    can pick different models without rewriting persistent state.
    """
    creds = await _resolve_creds()
    if not creds.id_token:
        raise MCVError("MCV credentials not configured")
    headers = {
        **_HTTP_HEADERS_BASE,
        "x-id-token": creds.id_token,
        "authorization": f"Bearer {creds.bearer or creds.id_token}",
    }
    body = _build_payload(messages, creds, model_override=model)
    async with httpx.AsyncClient(timeout=timeout) as cli:
        async with cli.stream("POST", creds.api_url, headers=headers, content=body) as resp:
            if resp.status_code != 200:
                err_text = await resp.aread()
                snippet = err_text[:300].decode("utf-8", errors="replace") if err_text else ""
                raise MCVError(f"upstream HTTP {resp.status_code}: {snippet}")
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                content = line[5:].lstrip()
                if content == "[DONE]":
                    return
                try:
                    delta = json.loads(content)["choices"][0]["delta"].get("content", "")
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    yield delta


# ---------- public helpers ---------- #

async def chat(
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    system: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
    task: str | None = None,
) -> str:
    """Round-trip single-turn (or continued) chat. Returns the full reply.

    Pass ``history`` (list of ``{"role": "...", "content": "..."}``) to
    continue an existing conversation. The MCV persona is injected as the
    first user-side message because the proxy doesn't formally support a
    ``system`` role.
    """
    sys_prompt = (system or MCV_SYSTEM_PROMPT_AR).strip()
    msgs: list[dict[str, str]] = []
    # OpenAI-compatible proxy: the system role works through OpenRouter,
    # but we *also* prepend a user/assistant pair as a strong fallback so
    # the persona survives even if the underlying provider drops the
    # ``system`` message.
    msgs.append({"role": "system", "content": sys_prompt})
    msgs.append({
        "role": "user",
        "content": (
            "عرّفني بنفسك بإيجاز قبل ما نبدأ."
        ),
    })
    msgs.append({
        "role": "assistant",
        "content": (
            "أنا MCV، مساعد منصة تيك زوم لاستضافة بوتات تلجرام. "
            "بساعد المستخدمين في تحويل وتعديل وفهم أكواد البوتات، "
            "وبجاوب أسئلتهم بنبرة مصرية حلوة وفيها هزار خفيف."
        ),
    })
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": user_text})

    chosen = model
    if not is_valid_model(chosen) and task:
        chosen = await resolve_model_for(task)
    out: list[str] = []
    async for chunk in _stream(msgs, timeout=timeout, model=chosen):
        out.append(chunk)
    return "".join(out).strip()


_CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def extract_code_block(text: str, prefer_lang: str | None = None) -> tuple[str, str]:
    """Return ``(language, code)`` from the most relevant fenced block.

    If ``prefer_lang`` is set, pick the *largest* block whose language tag
    matches (case-insensitive). Otherwise return the largest block overall.
    We pick by size to avoid grabbing a 5-line example over the real file.
    Returns ``("", "")`` when no code block exists.
    """
    blocks = list(_CODE_BLOCK_RE.finditer(text or ""))
    if not blocks:
        return "", ""
    if prefer_lang:
        target = prefer_lang.lower()
        # Some bots emit "python3", "py", etc.
        aliases = {
            "python": ("python", "python3", "py"),
            "node": ("javascript", "js", "node", "typescript", "ts"),
            "php": ("php",),
        }
        accepted = aliases.get(target, (target,))
        matching = [m for m in blocks if (m.group("lang") or "").lower() in accepted]
        if matching:
            best = max(matching, key=lambda m: len(m.group("body")))
            return best.group("lang") or target, best.group("body").rstrip() + "\n"
    # Fallback: largest block of any language.
    best = max(blocks, key=lambda m: len(m.group("body")))
    return (best.group("lang") or ""), best.group("body").rstrip() + "\n"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort balanced-brace extraction of the first JSON object.

    The naive regex ``\\{[\\s\\S]+?\\}`` matches the *smallest* ``{...}``
    substring, which falls apart whenever the JSON's values contain
    code with their own braces. We scan manually instead: starting from
    each ``{`` we track depth, skipping over string literals so that
    braces inside strings don't fool us, then try ``json.loads`` on the
    balanced slice. The first slice that parses wins.
    """
    if not text:
        return None
    s = text
    i = 0
    n = len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[i:j + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        i += 1
    return None


# Markers that indicate Python source contains the *runtime* of a
# Telegram bot, not just a stub of constants/imports. We use a
# generous superset so any reasonable implementation passes.
_BOT_RUNTIME_MARKERS: tuple[str, ...] = (
    "infinity_polling",
    "start_polling",
    "run_polling",
    ".polling(",
    "executor.start_polling",
    "dp.run_polling",
    "application.run_polling",
    "updater.start_polling",
    "asyncio.run(main",
    "asyncio.run(dp",
    "@bot.message_handler",
    "@dp.message",
    "@dp.message_handler",
)


def looks_like_complete_bot(code: str) -> bool:
    """Heuristically check that ``code`` is a runnable Telegram bot.

    Requires at least one import of a Telegram framework AND at least
    one runtime marker (polling call, dispatcher, message handler
    decorator). Used to reject AI replies that gave us only a header.
    """
    if not code or len(code) < 200:
        return False
    has_import = any(
        kw in code
        for kw in (
            "import telebot",
            "from telebot",
            "import aiogram",
            "from aiogram",
            "import telegram",
            "from telegram",
        )
    )
    has_runtime = any(marker in code for marker in _BOT_RUNTIME_MARKERS)
    return has_import and has_runtime


def build_fallback_bot(features: list[str]) -> str:
    """Generate a deterministic, runnable Python telebot from features.

    Used as a last-resort safety net so the wizard never produces a
    non-runnable stub. The result is a real bot with `/start` and
    `/help` and a feature list, which the user can then ask MCV to
    extend.

    Built via line-by-line assembly (rather than a string template)
    so we never have to juggle competing escape rules between Python
    string literals, f-strings, ``.format`` placeholders, and the
    Arabic content embedded in the generated file.
    """
    cleaned = [str(f).strip() for f in (features or []) if str(f).strip()]
    if not cleaned:
        cleaned = ["\u0628\u0648\u062a \u0628\u0633\u064a\u0637 \u0644\u0644\u062a\u062c\u0631\u0628\u0629"]
    features_literal = json.dumps(cleaned, ensure_ascii=False)
    lines = [
        '"""MCV fallback bot template.',
        "",
        "This file is generated automatically when the AI could not",
        "produce a full bot. It is a working /start and /help bot",
        "built on pyTelegramBotAPI (telebot). Edit it from the MCV",
        "tab to add features.",
        '"""',
        "import telebot",
        "",
        'BOT_TOKEN = "REPLACE_ME"',
        "",
        'bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")',
        "",
        "FEATURES = " + features_literal,
        "",
        "",
        '@bot.message_handler(commands=["start"])',
        "def on_start(m):",
        "    bullets = chr(10).join('\\u2022 ' + str(f) for f in FEATURES)",
        '    intro = "\\u270b \\u0623\\u0647\\u0644\\u0627\\u064b! \\u0623\\u0646\\u0627 \\u0628\\u0648\\u062a \\u0635\\u0646\\u0639\\u062a\\u0647 MCV."',
        '    header = "\\u0627\\u0644\\u0641\\u0643\\u0631\\u0629:"',
        '    footer = "\\u0627\\u062c\\u0631\\u0628 /help"',
        '    text = intro + chr(10) + chr(10) + header + chr(10) + bullets + chr(10) + chr(10) + footer',
        "    bot.reply_to(m, text)",
        "",
        "",
        '@bot.message_handler(commands=["help"])',
        "def on_help(m):",
        '    msg = "\\u2728 \\u0627\\u0644\\u0623\\u0648\\u0627\\u0645\\u0631:" + chr(10) + "/start" + chr(10) + "/help"',
        "    bot.reply_to(m, msg)",
        "",
        "",
        'if __name__ == "__main__":',
        '    print("MCV bot polling...", flush=True)',
        "    bot.infinity_polling(timeout=20, long_polling_timeout=20)",
        "",
    ]
    return "\n".join(lines)


# ---------- domain-specific prompts ---------- #

async def transpile_to_python(source: str, *, source_lang: str) -> str:
    """Convert a PHP/Node bot's source into a working Python script.

    Returns the Python source as a string. Raises :class:`MCVError` if
    the AI couldn't produce a valid block.
    """
    src_label = {"php": "PHP", "node": "Node.js / JavaScript"}.get(source_lang, source_lang)
    instr = (
        f"دي شفرة بوت تلجرام مكتوبة بـ {src_label}.\n"
        "حوّلها لبايثون كاملة شغّالة باستخدام مكتبة pyTelegramBotAPI (telebot) أو aiogram.\n\n"
        "قواعد:\n"
        "• خلّي التوكن في متغير BOT_TOKEN = \"REPLACE_ME\" (الكود الحقيقي عند المستخدم).\n"
        "• حافظ على نفس وظائف البوت بقدر الإمكان.\n"
        "• استخدم Long Polling لو الكود الأصلي ما كانش webhook صريح.\n"
        "• اجعل الكود واضحاً وقابلاً للقراءة.\n"
        "• ارجّع الكود في code block واحد فقط بالـ language tag ```python.\n\n"
        f"الكود الأصلي:\n```{source_lang}\n{source}\n```"
    )
    reply = await chat(
        instr,
        system=MCV_SYSTEM_PROMPT_AR + "\n\nالمهمة الحالية: تحويل كود لبايثون فقط.",
    )
    _, code = extract_code_block(reply, prefer_lang="python")
    if not code.strip():
        raise MCVError("AI did not return a Python code block")
    return code


@dataclass
class BotIntel:
    """Structured AI analysis of an uploaded bot."""

    purpose: str           # e.g. "بوت تنزيل فيديوهات يوتيوب"
    bot_type: str          # e.g. "downloader" | "music" | "ai" | "admin" | "tools" | "unknown"
    description: str       # 2-3 sentences in Arabic
    suggestions: list[str] # list of short Arabic suggestions

    def as_html(self) -> str:
        import html as _html

        sugg = "\n".join(f"• {_html.escape(s, quote=False)}" for s in self.suggestions) \
            or "<i>لا اقتراحات لحد دلوقتي.</i>"
        return (
            f"🤖 <b>نوع البوت:</b> {_html.escape(self.purpose, quote=False)} "
            f"<i>({_html.escape(self.bot_type, quote=False)})</i>\n\n"
            f"📝 <b>الوصف:</b>\n<blockquote>"
            f"{_html.escape(self.description, quote=False)}</blockquote>\n\n"
            f"💡 <b>اقتراحات MCV:</b>\n{sugg}"
        )


async def detect_bot_purpose(source: str, *, language: str = "python",
                              file_name: str = "") -> BotIntel:
    """Classify what the uploaded bot does + suggest improvements.

    Returns a :class:`BotIntel`. Best-effort: if parsing the AI's JSON
    fails we still salvage a free-form description.
    """
    instr = (
        f"شوف كود البوت ده ({language}) واطلع لي JSON واحد فقط بدون أي شرح إضافي:\n"
        '{"purpose":"…","bot_type":"…","description":"…","suggestions":["…","…","…"]}\n\n'
        "purpose: جملة مختصرة عن غرض البوت بالعربي.\n"
        'bot_type: واحد من: downloader, music, ai, admin, tools, broadcast, game, '
        'shop, chat, unknown.\n'
        "description: جملتين أو ثلاث بالعربي تشرح إيه اللي البوت بيعمله.\n"
        "suggestions: قائمة من 3 إلى 5 اقتراحات لتطوير البوت بالعربي، كل اقتراح سطر واحد.\n\n"
        f"اسم الملف: {file_name or '—'}\n"
        f"```{language}\n{source[:18000]}\n```"
    )
    reply = await chat(instr)
    # Try to extract JSON anywhere in the reply.
    obj: dict[str, Any] | None = None
    candidates = []
    for m in re.finditer(r"\{[\s\S]+?\}", reply):
        candidates.append(m.group(0))
    candidates.sort(key=len, reverse=True)
    for raw in candidates:
        try:
            obj = json.loads(raw)
            break
        except ValueError:
            continue
    if not isinstance(obj, dict):
        return BotIntel(
            purpose="بوت تلجرام",
            bot_type="unknown",
            description=(reply.strip()[:400] or "لم يتم تحليل البوت."),
            suggestions=[],
        )
    suggestions_raw = obj.get("suggestions") or []
    if isinstance(suggestions_raw, str):
        suggestions = [s.strip() for s in suggestions_raw.split("\n") if s.strip()]
    elif isinstance(suggestions_raw, list):
        suggestions = [str(s).strip() for s in suggestions_raw if str(s).strip()]
    else:
        suggestions = []
    return BotIntel(
        purpose=str(obj.get("purpose") or "بوت تلجرام")[:160],
        bot_type=str(obj.get("bot_type") or "unknown")[:32],
        description=str(obj.get("description") or "")[:1000],
        suggestions=suggestions[:6],
    )


@dataclass
class AISecurityReview:
    """AI second-opinion on whether an uploaded file is malicious."""

    safe: bool
    confidence: int  # 0..100
    reasons: list[str]  # what tipped the verdict

    def as_html(self) -> str:
        import html as _html

        emoji = "✅" if self.safe else "🚨"
        verdict = "آمن" if self.safe else "مشبوه"
        bullets = "\n".join(f"• {_html.escape(r, quote=False)}" for r in self.reasons) \
            or "<i>لا تفاصيل إضافية.</i>"
        return (
            f"{emoji} <b>تقييم MCV الأمني:</b> {verdict} "
            f"({self.confidence}%)\n{bullets}"
        )


async def review_for_malicious(source: str, *, language: str = "python") -> AISecurityReview:
    """Use the AI as a second opinion on whether a file is malicious."""
    instr = (
        f"شوف الكود ده ({language}) وقولي JSON بس بدون شرح:\n"
        '{"safe": true|false, "confidence": 0..100, "reasons": ["…","…"]}\n\n'
        "اعتبر الكود مشبوهًا لو فيه: قراءة ملفات النظام (.env / passwd / ssh / TikZoom)، "
        "استخراج Environment Variables، تشغيل أوامر شل، eval/exec، تشفير ديناميكي للكود، "
        "إرسال بيانات لـ webhooks غير تلجرام، فحص شبكة، أو محاولة الوصول لقاعدة البيانات.\n\n"
        f"```{language}\n{source[:18000]}\n```"
    )
    try:
        reply = await chat(instr, timeout=90.0)
    except MCVError as exc:
        logger.info("MCV review skipped: %s", exc)
        return AISecurityReview(safe=True, confidence=0, reasons=[f"AI offline: {exc}"])
    obj = _extract_json_object(reply)
    if not isinstance(obj, dict):
        return AISecurityReview(safe=True, confidence=0, reasons=["AI لم يرد بصيغة صالحة"])
    return AISecurityReview(
        safe=bool(obj.get("safe", True)),
        confidence=int(obj.get("confidence") or 0),
        reasons=[str(x) for x in (obj.get("reasons") or [])][:8],
    )


async def modify_bot_code(source: str, *, instructions: str,
                           language: str = "python") -> str:
    """Apply ``instructions`` to ``source`` and return the new full file.

    Uses the dedicated coder system prompt (same one as ``generate_bot``)
    so the edited output stays production-grade. We try the JSON form
    first, then fall back to the largest fenced code block.
    """
    instr = (
        "عدّل الكود ده حسب التعليمات وارجع الملف الكامل بعد التعديل.\n\n"
        f"التعليمات من المستخدم:\n{instructions}\n\n"
        f"الكود الحالي:\n```{language}\n{source}\n```\n\n"
        "ارجع JSON واحد فقط بالشكل التالي بدون أي نص خارج:\n"
        '{"name": "<keep_or_improve_snake_name>", "code": "<الكود كاملاً>"}'
    )
    sys_prompt = MCV_CODER_PROMPT_AR if language == "python" else None
    reply = await chat(instr, system=sys_prompt, timeout=240.0, task="code")
    code = ""
    obj = _extract_json_object(reply)
    if isinstance(obj, dict):
        code = str(obj.get("code") or "").strip()
    if not code or len(code) < 200:
        _, blk = extract_code_block(reply, prefer_lang=language)
        if blk and len(blk) > len(code):
            code = blk
    if not code.strip():
        raise MCVError("AI did not return a code block for the modified file")
    return code


def _embed_token_into_code(code: str, token: str) -> str:
    """Replace any placeholder with the real token. Idempotent."""
    if not token:
        return code
    code = code.replace("REPLACE_ME", token)
    code = re.sub(
        r'BOT_TOKEN\s*=\s*["\\\'](YOUR[_ ]?TOKEN[_ ]?HERE|TOKEN|TELEGRAM[_ ]?TOKEN|<.*?>)["\\\']',
        f'BOT_TOKEN = "{token}"', code, flags=re.IGNORECASE,
    )
    return code


def _parse_wizard_description(description: str) -> tuple[str, list[str]]:
    """Split a wizard-built description into ``(purpose, features)``.

    The wizard builds description text like::

        الفكرة الأساسية: بوت يجيب أسعار العملات
        - ميزة: زرار /price يجيب السعر الحالي
        - ميزة: زرار /history يجيب آخر ٢٤ ساعة

    We pull the purpose (first line) and the bullet features separately
    so the prompt can demand a dedicated handler for *each* feature.
    """
    if not description:
        return "", []
    lines = [ln.strip() for ln in description.splitlines() if ln.strip()]
    if not lines:
        return "", []
    purpose = lines[0]
    # Strip a leading "الفكرة الأساسية:" / "Purpose:" if present.
    for prefix in ("الفكرة الأساسية:", "الفكرة:", "Purpose:", "Idea:"):
        if purpose.startswith(prefix):
            purpose = purpose[len(prefix):].strip()
            break
    features: list[str] = []
    for ln in lines[1:]:
        for prefix in ("- ميزة:", "• ميزة:", "ميزة:", "-", "•", "*"):
            if ln.startswith(prefix):
                ln = ln[len(prefix):].strip()
                break
        if ln:
            features.append(ln)
    return purpose, features


def _render_generate_bot_user_message(
    description: str, *, attempt: int, embed_token: str | None,
) -> str:
    """Build the user-side instruction for the coder system prompt.

    ``attempt`` is 1-based: each retry adds extra emphasis to nudge the
    model away from whatever it did wrong last time.
    """
    purpose, features = _parse_wizard_description(description)
    feat_block = ""
    if features:
        bullets = "\n".join(
            f"  {i + 1}) {f} → لازم يبقى ليها أمر أو زرار مخصوص."
            for i, f in enumerate(features)
        )
        feat_block = "\n\nالميزات (كل ميزة handler مستقل):\n" + bullets
    token_note = ""
    if embed_token:
        token_note = (
            "\n\nملحوظة: التوكن جاهز عندي. خلي ``BOT_TOKEN = \"REPLACE_ME\"`` "
            "زي ما هو وأنا هحطه مكانه."
        )
    base = (
        "اكتب بوت تلجرام احترافي ٢٠٢٦ بالكامل بايثون. الفكرة الأساسية:\n"
        f"{purpose or description}"
        f"{feat_block}{token_note}\n\n"
        "تأكد إن:\n"
        "• كل ميزة من اللي فوق ليها handler حقيقي بيشتغل (مش ذكر في /help فقط).\n"
        "• الـ /start بيشرح للمستخدم البوت بيعمل إيه ويعرض قايمة بالأوامر.\n"
        "• الـ /help بيشرح كل أمر سطر سطر.\n"
        "• حط ReplyKeyboardMarkup فيه أزرار للميزات الرئيسية في /start.\n"
        "• فيه ``logging`` وفيه error handling داخل كل handler.\n"
        "• الكود نظيف، منسّق، فيه docstrings وتعليقات قصيرة بالعربي.\n"
    )
    if attempt == 2:
        base += (
            "\n!! المرة اللي فاتت كان الكود ناقص. ركّز هذه المرة على "
            "إنه يحتوي **فعلاً** على `bot.infinity_polling(...)` في الآخر و "
            "على message_handler لكل ميزة. ارجع JSON صحيح بدون نص خارجي."
        )
    elif attempt >= 3:
        base += (
            "\n!! المحاولة الأخيرة. لو الميزات معقدة، اعمل minimal viable bot "
            "فيه /start و /help و على الأقل handler واحد لكل ميزة، حتى لو "
            "بيرد رسالة بسيطة. المهم البوت يشتغل ويرد على المستخدم."
        )
    return base


async def generate_bot(description: str, *, embed_token: str | None = None) -> tuple[str, str]:
    """Generate a fresh Python bot from a free-text description.

    Returns ``(file_name, code)``. The file name is a sane slug derived
    from the request so callers can save it directly. If ``embed_token``
    is provided we replace any placeholder so the generated file is
    ready to run without an extra manual edit.

    Robust against AI quirks:
    * Uses a dedicated coder system prompt that disables the joking
      persona and demands production-grade output with one handler per
      requested feature.
    * Tries up to 3 model calls with stricter framing each retry.
    * Parses JSON via balanced-brace scanning, then falls back to the
      largest fenced Python block in the reply.
    * If the result still doesn't look like a complete bot (no polling
      / no handlers), assembles a deterministic fallback template so
      we *never* ship a 4-line stub to the runner.
    """
    last_err: str | None = None
    name = ""
    code = ""
    for attempt in (1, 2, 3):
        instr = _render_generate_bot_user_message(
            description, attempt=attempt, embed_token=embed_token,
        )
        try:
            # Use the dedicated coder prompt, not the chat persona.
            # 240s timeout — Opus needs room to produce a full bot.
            reply = await chat(
                instr, system=MCV_CODER_PROMPT_AR, timeout=240.0,
                task="code",
            )
        except MCVError as exc:
            last_err = str(exc)
            logger.info("generate_bot attempt %d failed: %s", attempt, exc)
            continue
        obj = _extract_json_object(reply)
        cand_name = ""
        cand_code = ""
        if isinstance(obj, dict):
            cand_name = str(obj.get("name") or "").strip()
            cand_code = str(obj.get("code") or "").strip()
        # If JSON gave us no usable code, try the largest fenced block.
        if not cand_code or len(cand_code) < 200:
            _, blk = extract_code_block(reply, prefer_lang="python")
            if blk and len(blk) > len(cand_code):
                cand_code = blk
        if cand_code and looks_like_complete_bot(cand_code):
            name, code = cand_name, cand_code
            break
        last_err = (
            f"incomplete (len={len(cand_code)}, looks_complete="
            f"{looks_like_complete_bot(cand_code)})"
        )
        logger.info("generate_bot attempt %d incomplete: %s", attempt, last_err)

    if not code or not looks_like_complete_bot(code):
        logger.warning("generate_bot falling back to template: %s", last_err)
        _, features = _parse_wizard_description(description)
        code = build_fallback_bot(features or [description or "بوت بسيط"])
        if not name:
            name = "mcv_bot"

    if not name:
        name = "mcv_bot_" + str(int(time.time()))
    # Sanitize the filename — letters, digits, underscores only.
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or "mcv_bot"
    if not name.endswith(".py"):
        name += ".py"
    # Embed the user's real token if they already gave one, so the file
    # is immediately runnable.
    if embed_token:
        code = _embed_token_into_code(code, embed_token)
    return name, code


# ---------- multi-turn bot-creation wizard helpers ---------- #

# Done-like phrases — when the user types any of these during the
# requirements loop we stop collecting features and ship the bot.
_DONE_PHRASES_AR = (
    "خلاص", "كده تمام", "تمام كده", "خلصت", "كفاية", "بس كده",
    "اكتبه", "احفظه", "ابني", "ابدا", "ابدأ", "اعمله", "نزله",
    "ولعها", "ولعها بقى", "نزّلها", "اعمل بقى", "خلاصني", "ماشي",
    "ok", "okay", "done", "build", "go", "ready", "finish",
    "اعمل البوت", "اعملي البوت", "ابني البوت",
)

# Exit phrases — leave the entire conversation.
_EXIT_PHRASES_AR = (
    "خروج", "اخرج", "خروج بقى", "بيي", "باي", "مع السلامة",
    "exit", "quit", "bye", "stop", "/exit", "/quit",
)


def is_done_phrase(text: str) -> bool:
    """True when the user signals they are done adding features."""
    t = (text or "").strip().lower().rstrip(".!؟?،,")
    if not t:
        return False
    return t in _DONE_PHRASES_AR


def is_exit_phrase(text: str) -> bool:
    """True when the user wants to leave the assistant entirely."""
    t = (text or "").strip().lower().rstrip(".!؟?،,")
    if not t:
        return False
    return t in _EXIT_PHRASES_AR


async def wizard_acknowledge(feature: str, features_so_far: int) -> str:
    """Generate a short, witty Egyptian Arabic reply confirming a feature
    and asking whether the user wants to add more. We keep this offline
    (no AI call) so the wizard stays snappy and works even when the AI
    proxy is rate-limited.
    """
    import random

    acks = [
        f"تمام، ضفت «{feature}». في حاجة تانية تضيفها ولا نطلع نشغل البوت؟",
        f"«{feature}» — ماشي 👌 في ميزة تانية في بالك؟",
        f"حلو، «{feature}» على القائمة. عاوز تضيف غيرها ولا نقفل؟",
        f"تمام يا معلم، «{feature}» في القايمة. حاجة تانية ولا خلاص؟",
        f"اتسجلت «{feature}» ✨ أي ميزة تانية ولا نبدأ؟",
    ]
    if features_so_far >= 4:
        acks = [
            f"«{feature}» اتضافت. بقت {features_so_far} ميزات — تحب تضيف تانية ولا نولّع؟",
            f"ضفت «{feature}». في غيرها ولا خلاص بقى؟",
        ]
    return random.choice(acks)


async def project_analyze(
    *,
    tree: list[str],
    sample_sources: dict[str, str],
) -> dict[str, Any]:
    """Ask MCV to identify the main file of a multi-file bot project.

    ``tree`` is the relative file list. ``sample_sources`` maps file
    paths to (truncated) source bodies for the top candidates.

    Returns a dict with keys: main_file, language, run_mode,
    dependencies (list[str]), notes (str).
    """
    samples_block: list[str] = []
    for path, body in list(sample_sources.items())[:6]:
        head = body[:6000]
        samples_block.append(f"\n## {path}\n```\n{head}\n```")
    instr = (
        "هذا مشروع بوت تلجرام مرفوع بعدة ملفات. مهمتك تحدد إيه الملف الرئيسي "
        "اللي نشغّله، إيه لغته، وأي مكتبات لازم نثبتها.\n\n"
        "ارجّع JSON واحد فقط بدون أي شرح بهذا الشكل:\n"
        '{"main_file":"path/relative","language":"python|node|php",'
        '"run_mode":"polling|webhook","dependencies":["lib1","lib2"],'
        '"notes":"شرح قصير بالعربي"}\n\n'
        "اختر دائماً ملفًا فيه نقطة دخول حقيقية (مثل: if __name__ == '__main__' "
        "في Python، أو entry في package.json لـ Node، أو ملف PHP يحتوي ربط webhook).\n\n"
        f"شجرة الملفات:\n{chr(10).join('- ' + p for p in tree[:120])}\n"
        f"\nعينات من أهم الملفات:{''.join(samples_block)}"
    )
    reply = await chat(instr, timeout=180.0)
    obj: dict[str, Any] | None = None
    for m in re.finditer(r"\{[\s\S]+?\}", reply):
        try:
            obj = json.loads(m.group(0))
            break
        except ValueError:
            continue
    if not isinstance(obj, dict):
        return {
            "main_file": "",
            "language": "python",
            "run_mode": "polling",
            "dependencies": [],
            "notes": reply.strip()[:500],
        }
    return {
        "main_file": str(obj.get("main_file") or "")[:300],
        "language": str(obj.get("language") or "python"),
        "run_mode": str(obj.get("run_mode") or "polling"),
        "dependencies": [str(x).strip()
                         for x in (obj.get("dependencies") or [])
                         if str(x).strip()][:30],
        "notes": str(obj.get("notes") or "")[:800],
    }


__all__ = [
    "MCVError",
    "MCV_SYSTEM_PROMPT_AR",
    "chat",
    "extract_code_block",
    "transpile_to_python",
    "detect_bot_purpose",
    "review_for_malicious",
    "modify_bot_code",
    "generate_bot",
    "BotIntel",
    "AISecurityReview",
    "update_credentials",
    "get_credentials_status",
    "is_done_phrase",
    "is_exit_phrase",
    "wizard_acknowledge",
    "project_analyze",
]
