"""BotRunner: launch and supervise user-uploaded bots in Python / PHP / Node.js.

Each hosted bot runs as its own subprocess. The subprocess is started with the
following environment variables so the bot script can pick them up:

    BOT_TOKEN     — the bot's Telegram token
    PORT          — the local TCP port assigned for webhook mode (if used)
    WEBHOOK_URL   — public webhook URL for this bot
    WEBHOOK_PATH  — path component (without host)
    PLATFORM      — set to "tikzoom"

Whether the bot uses polling or webhook is up to its own code; we just supply
the environment hints. The platform sets the Telegram webhook for the bot if
`use_webhook=True`, otherwise leaves it unset (polling mode).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings


# Regexes for common runtime "missing module" errors.
_RE_PY_MISSING = re.compile(
    r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]"
)
_RE_PY_IMPORT_ERR = re.compile(
    r"ImportError: cannot import name ['\"][^'\"]+['\"] from ['\"]([^'\"]+)['\"]"
)
_RE_NODE_MISSING = re.compile(
    r"Cannot find module ['\"]([^'\"]+)['\"]"
)


def _extract_missing_python_module(log_tail: str) -> str | None:
    """Return the *top-level* missing module name, or ``None``."""
    m = _RE_PY_MISSING.search(log_tail)
    if m:
        return m.group(1).split(".")[0]
    m = _RE_PY_IMPORT_ERR.search(log_tail)
    if m:
        return m.group(1).split(".")[0]
    return None


def _extract_missing_node_module(log_tail: str) -> str | None:
    m = _RE_NODE_MISSING.search(log_tail)
    if m:
        name = m.group(1)
        # Node "Cannot find module 'X/Y'" — only install the package root.
        if name.startswith("@"):
            parts = name.split("/", 2)
            return "/".join(parts[:2])
        return name.split("/")[0]
    return None

logger = logging.getLogger(__name__)


SUPPORTED_LANGUAGES = ("python", "php", "node")
EXT_TO_LANG = {".py": "python", ".php": "php", ".js": "node", ".mjs": "node", ".cjs": "node"}


def detect_language(file_name: str) -> str | None:
    ext = Path(file_name).suffix.lower()
    return EXT_TO_LANG.get(ext)


def _which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def get_runner_command(language: str, file_path: str, *, bot_id: int = 0) -> list[str] | None:
    """Build the command line to launch a bot in the given language.

    For Python we go through ``app/sandbox_shim.py`` which sets up a
    best-effort filesystem sandbox before running the user's script — this
    keeps casual code from peeking at platform data files even when all bots
    share the same Windows account.
    """
    if language == "python":
        # Prefer the platform's own interpreter so hosted bots inherit the
        # exact site-packages we ``pip install`` deps into. Fall back to the
        # first ``python`` on PATH only if sys.executable is somehow missing.
        py = sys.executable or _which("python3", "python")
        if not py:
            return None
        shim = Path(__file__).resolve().parent / "sandbox_shim.py"
        if shim.is_file():
            return [py, "-u", str(shim), str(bot_id), file_path]
        # No shim found — fall back to direct invocation. This shouldn't
        # happen in production but we degrade gracefully.
        return [py, "-u", file_path]
    if language == "node":
        node = _which("node")
        if not node:
            return None
        return [node, file_path]
    if language == "php":
        php = _which("php")
        if not php:
            return None
        # Run as long-running CLI script (the user's script is responsible for serving HTTP if any).
        return [php, file_path]
    return None


# Environment variables that contain platform secrets and must NEVER be
# inherited by user-uploaded bots. This is a deny-list so missing entries fail
# safe (the user's bot may not have something it expects, but it can't read
# our keys). Names are matched case-insensitively, with prefix matching for
# anything ending in ``_``.
_SECRET_ENV_DENYLIST = (
    "BOT_TOKEN",  # platform's own token; we set the bot's own token explicitly below
    "TIKZOOM_",
    "ENCRYPTION_KEY",
    "WEBHOOK_SECRET",
    "ADMIN_IDS",
    "MAIN_BOT_TOKEN",
    "DATABASE_URL",
    "PUBLIC_BASE_URL",
    "FORCE_SUB_CHANNELS",
    "BASE_DIR",
    "DATA_PATH",
    "BOTS_PATH",
)


def _build_safe_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with platform secrets removed.

    We *want* to keep PATH / SYSTEMROOT / TEMP / USERPROFILE / locale vars so
    pip-installed deps and standard libraries continue to work; we just strip
    anything that looks like a TikZoom secret. We also inject
    ``TIKZOOM_SANDBOX_BLOCKED_ROOTS`` so the Python shim knows which
    filesystem prefixes to refuse.
    """
    safe: dict[str, str] = {}
    for k, v in os.environ.items():
        kU = k.upper()
        if kU in _SECRET_ENV_DENYLIST:
            continue
        if any(kU.startswith(p) for p in _SECRET_ENV_DENYLIST if p.endswith("_")):
            continue
        safe[k] = v
    # Tell the Python sandbox shim which directories belong to the platform
    # so it can deny ``open()`` / ``os.open()`` calls into them.
    s = get_settings()
    blocked = []
    for p in (s.data_path, s.bots_path):
        try:
            blocked.append(str(Path(p).resolve()))
        except OSError:
            continue
    # Install root (parent of the data dir) — usually ``C:\TikZoom``.
    try:
        install_root = Path(s.data_path).resolve().parent
        blocked.append(str(install_root))
    except OSError:
        pass
    if blocked:
        safe["TIKZOOM_SANDBOX_BLOCKED_ROOTS"] = os.pathsep.join(blocked)
    return safe


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def allocate_port(used: set[int]) -> int | None:
    s = get_settings()
    for p in range(s.hosted_port_start, s.hosted_port_end + 1):
        if p in used:
            continue
        if _is_port_free(p):
            return p
    return None


@dataclass
class RunResult:
    pid: int | None
    error: str | None


class BotRunner:
    """Launches and supervises hosted-bot subprocesses (one per HostedBot)."""

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}  # hosted_bot_id -> Popen
        self._lock = asyncio.Lock()

    @property
    def used_ports(self) -> set[int]:
        # The set of allocated ports is tracked by HostedBot.port in DB; this
        # method is a convenience helper for re-allocations within a session.
        return set()

    def is_running(self, bot_id: int) -> bool:
        proc = self._procs.get(bot_id)
        return proc is not None and proc.poll() is None

    def get_pid(self, bot_id: int) -> int | None:
        proc = self._procs.get(bot_id)
        if proc and proc.poll() is None:
            return proc.pid
        return None

    async def _mark_crashed(self, bot_id: int, message: str | None) -> None:
        """Persist a crashed status + tail of the failure into the DB.

        Best-effort: errors are logged but never propagate, because this
        runs from supervisor background tasks where we don't want to
        kill the loop on a transient DB problem.
        """
        try:
            from .repo import update_bot_status

            tail = (message or "").strip()
            # Keep the snippet short — the column is for a single error.
            if len(tail) > 600:
                tail = tail[-600:]
            await update_bot_status(
                bot_id, status="crashed", last_error=tail or "process exited early",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not mark bot=%s crashed: %s", bot_id, exc)

    async def start(
        self,
        *,
        bot_id: int,
        language: str,
        file_path: str,
        token: str,
        port: int | None,
        webhook_url: str | None,
        cwd: str | None = None,
    ) -> RunResult:
        async with self._lock:
            if self.is_running(bot_id):
                return RunResult(pid=self._procs[bot_id].pid, error=None)
            cmd = get_runner_command(language, file_path, bot_id=bot_id)
            if cmd is None:
                return RunResult(pid=None, error=f"runtime for '{language}' not installed")
            # Build a clean env from scratch — strip platform secrets so a
            # malicious bot can't read them via ``os.environ``.
            env = _build_safe_env()
            env["BOT_TOKEN"] = token
            env["PLATFORM"] = "tikzoom"
            if port is not None:
                env["PORT"] = str(port)
            if webhook_url:
                env["WEBHOOK_URL"] = webhook_url
                from urllib.parse import urlparse

                env["WEBHOOK_PATH"] = urlparse(webhook_url).path or "/"
            log_path = Path(get_settings().data_path) / "logs" / f"bot_{bot_id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_path, "ab", buffering=0)
            kwargs: dict = {
                "stdout": log_fp,
                "stderr": log_fp,
                "env": env,
                "cwd": cwd or str(Path(file_path).parent),
            }
            if sys.platform != "win32":
                kwargs["preexec_fn"] = os.setsid
            else:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
            except Exception as exc:  # broad catch: subprocess failures are varied
                log_fp.close()
                return RunResult(pid=None, error=str(exc))
            self._procs[bot_id] = proc
            logger.info("started bot_id=%s pid=%s lang=%s", bot_id, proc.pid, language)
            return RunResult(pid=proc.pid, error=None)

    async def start_supervised(
        self,
        *,
        bot_id: int,
        language: str,
        file_path: str,
        token: str,
        port: int | None,
        webhook_url: str | None,
        cwd: str | None = None,
        max_retries: int = 6,
        check_after: float = 4.0,
    ) -> RunResult:
        """Start a bot, then watch its log for ``ModuleNotFoundError``.

        If the bot exits within ``check_after`` seconds and the log shows a
        missing-module error (Python or Node.js), pip-install / npm-install
        the missing package and restart the bot. We loop up to ``max_retries``
        times before giving up. The watcher runs in the background so the
        caller doesn't block — the returned :class:`RunResult` is for the
        *initial* spawn only.

        Always stops any existing process for ``bot_id`` first so callers can
        safely use this for re-uploads / re-runs without first calling
        :meth:`stop`.
        """
        if self.is_running(bot_id):
            await self.stop(bot_id)
        result = await self.start(
            bot_id=bot_id, language=language, file_path=file_path,
            token=token, port=port, webhook_url=webhook_url, cwd=cwd,
        )
        if result.error:
            return result

        # Spin up an asyncio task that supervises the bot. If it exits early
        # with a "ModuleNotFoundError: No module named 'X'" (or Node's
        # "Cannot find module 'X'") we try to install the missing dep and
        # restart. The task captures the *current* set of arguments via
        # closure so subsequent restarts use the same configuration.
        async def _supervise() -> None:
            from .deps import install_missing_python_module, install_missing_node_module

            attempts = 0
            while attempts < max_retries:
                attempts += 1
                await asyncio.sleep(check_after)
                proc = self._procs.get(bot_id)
                if proc is None or proc.poll() is None:
                    # Still running — supervisor is done, healthy bot.
                    return
                # Process has exited. Read the recent log and look for a
                # known missing-module signature.
                log_path = Path(get_settings().data_path) / "logs" / f"bot_{bot_id}.log"
                tail = ""
                try:
                    with open(log_path, "rb") as f:  # noqa: ASYNC101 — small read
                        try:
                            f.seek(-8192, os.SEEK_END)
                        except OSError:
                            f.seek(0)
                        tail = f.read().decode("utf-8", "replace")
                except OSError:
                    pass

                missing: str | None = None
                fixed = False
                if language == "python":
                    missing = _extract_missing_python_module(tail)
                    if missing:
                        ok, _log = await install_missing_python_module(
                            missing, cwd=Path(file_path).parent,
                        )
                        if ok:
                            fixed = True
                            logger.info(
                                "bot=%s auto-installed python module %s",
                                bot_id, missing,
                            )
                elif language == "node":
                    missing = _extract_missing_node_module(tail)
                    if missing:
                        ok, _log = await install_missing_node_module(
                            missing, cwd=Path(file_path).parent,
                        )
                        if ok:
                            fixed = True
                            logger.info(
                                "bot=%s auto-installed node module %s",
                                bot_id, missing,
                            )
                if not fixed:
                    logger.info(
                        "bot=%s exited and no missing-module fix found (attempt %d/%d)",
                        bot_id, attempts, max_retries,
                    )
                    # The bot died early without a missing module. Most
                    # likely it has no polling loop, an invalid token,
                    # or hit an unhandled exception. Mark it crashed so
                    # the UI doesn't lie about it being "running".
                    await self._mark_crashed(bot_id, tail)
                    return
                # Restart with the same parameters.
                self._procs.pop(bot_id, None)
                restart = await self.start(
                    bot_id=bot_id, language=language, file_path=file_path,
                    token=token, port=port, webhook_url=webhook_url, cwd=cwd,
                )
                if restart.error:
                    logger.warning(
                        "bot=%s restart after auto-install failed: %s",
                        bot_id, restart.error,
                    )
                    await self._mark_crashed(bot_id, restart.error)
                    return
            logger.info("bot=%s supervisor giving up after %d attempts", bot_id, max_retries)
            await self._mark_crashed(
                bot_id,
                "supervisor gave up after %d restart attempts" % max_retries,
            )

        # Don't keep a reference — fire-and-forget background task.
        asyncio.create_task(_supervise())
        return result

    async def stop(self, bot_id: int) -> bool:
        async with self._lock:
            proc = self._procs.get(bot_id)
            if not proc:
                return True
            if proc.poll() is not None:
                self._procs.pop(bot_id, None)
                return True
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                self._procs.pop(bot_id, None)
                return True
            except Exception as exc:
                logger.warning("stop bot_id=%s: %s", bot_id, exc)
                try:
                    proc.kill()
                except Exception:
                    pass
                self._procs.pop(bot_id, None)
                return False

    async def stop_all(self) -> None:
        ids = list(self._procs.keys())
        for bid in ids:
            await self.stop(bid)


# Singleton
_runner: BotRunner | None = None


def get_runner() -> BotRunner:
    global _runner
    if _runner is None:
        _runner = BotRunner()
    return _runner
