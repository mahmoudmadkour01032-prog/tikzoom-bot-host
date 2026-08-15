"""Sandbox launcher used to run user-uploaded Python bots.

The platform spawns each hosted bot via:

    python -u app/sandbox_shim.py <bot_id> <user_script.py>

This shim sets up a *best-effort* Python-level sandbox before handing control
to the user's script. The goals are:

* Prevent the user's code from accidentally (or deliberately) reading the
  platform's data files (token DB, other users' bot directories, the main
  bot's environment, etc.).
* Force the user's working directory and ``sys.path`` to its own bot folder,
  so e.g. a ``open('platform.db')`` lookup goes nowhere useful.
* Strip platform secrets out of the environment that would otherwise be
  inherited from the parent process.

This is **not** a security boundary on its own — Python sandboxes can always
be bypassed via ``ctypes`` / native code. The real isolation should also
include OS-level controls (separate user account, ICACLS, etc.). The shim is
a defence-in-depth layer that catches naive mistakes and casual exfiltration
attempts.
"""
from __future__ import annotations

import builtins
import io
import os
import sys
from pathlib import Path

# ---------- argument parsing ---------- #

if len(sys.argv) < 3:
    sys.stderr.write("sandbox_shim: usage: <bot_id> <script>\n")
    sys.exit(2)

BOT_ID = sys.argv[1]
SCRIPT = Path(sys.argv[2]).resolve()
SANDBOX = SCRIPT.parent.resolve()

# ---------- chdir + sys.path ---------- #

try:
    os.chdir(str(SANDBOX))
except OSError as exc:  # noqa: BLE001
    sys.stderr.write(f"sandbox_shim: cannot chdir to {SANDBOX}: {exc}\n")
    sys.exit(2)

# Drop only the EXACT shim/app/repo-root entries from sys.path so the bot
# can't ``import app.security`` etc. We must NOT prefix-match here, because
# the venv's site-packages lives under the same repo root and we *do* need
# those to stay importable (telebot, aiogram, etc).
_shim_dir = Path(__file__).resolve().parent
_shim_root = _shim_dir.parent  # ``app/sandbox_shim.py`` -> project root
_blocked_path_exact = {str(_shim_dir), str(_shim_root), ""}
_clean_sys_path: list[str] = []
for _p in sys.path:
    if not _p:
        continue
    try:
        _resolved = str(Path(_p).resolve())
    except (OSError, RuntimeError):
        _clean_sys_path.append(_p)
        continue
    if _resolved in _blocked_path_exact:
        continue
    _clean_sys_path.append(_p)
sys.path = [str(SANDBOX), *_clean_sys_path]

# ---------- capture sandbox config BEFORE env scrub ---------- #
# The parent process tells us which absolute paths to block via
# ``TIKZOOM_SANDBOX_BLOCKED_ROOTS`` (path-list separated by ``os.pathsep``).
# We MUST read this var before scrubbing the environment, because the scrub
# wipes anything starting with ``TIKZOOM_``.
_BLOCKED_ROOTS_RAW = os.environ.pop("TIKZOOM_SANDBOX_BLOCKED_ROOTS", "")


def _resolve_blocked() -> list[Path]:
    candidates: list[str] = []
    if _BLOCKED_ROOTS_RAW:
        candidates.extend(c for c in _BLOCKED_ROOTS_RAW.split(os.pathsep) if c.strip())
    # Always include the well-known Windows install path as a safety net.
    candidates.append(r"C:\TikZoom")
    out: list[Path] = []
    seen: set[str] = set()
    for c in candidates:
        try:
            p = Path(c).resolve()
        except (OSError, RuntimeError):
            continue
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        out.append(p)
    return out


_BLOCKED_ROOTS: list[Path] = _resolve_blocked()

# ---------- environment scrub ---------- #
# These names commonly carry platform secrets; we wipe them so user code can't
# pick them up via ``os.environ``. The parent process explicitly forwards the
# subset the bot actually needs (BOT_TOKEN, PORT, WEBHOOK_URL, ...).
_BLOCKED_ENV_PREFIXES = (
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
for _k in list(os.environ.keys()):
    if any(_k.upper().startswith(p) for p in _BLOCKED_ENV_PREFIXES):
        os.environ.pop(_k, None)

# ---------- filesystem isolation ---------- #


def _is_blocked(path_like) -> bool:
    try:
        s = os.fspath(path_like)
    except TypeError:
        return False
    if not isinstance(s, (str, bytes)):
        return False
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False
    try:
        rp = Path(s).resolve()
    except (OSError, RuntimeError):
        # Path with too many symlinks or other resolution failures — be safe
        # and let the caller deal with the original error.
        return False
    # Always allow access INSIDE our own sandbox (even if it's nested under a
    # blocked root, e.g. C:\TikZoom\bots\<uid>\<sub>).
    try:
        rp.relative_to(SANDBOX)
        return False
    except ValueError:
        pass
    for prefix in _BLOCKED_ROOTS:
        try:
            rp.relative_to(prefix)
            return True
        except ValueError:
            continue
    return False


# Patch ``builtins.open`` -- this catches the vast majority of file-read
# attempts including ``open(..., 'r')``, ``open(..., 'rb')``, etc.
_real_open = builtins.open


def _safe_open(file, *args, **kwargs):  # noqa: ANN001, ANN201
    if _is_blocked(file):
        raise PermissionError(f"sandbox: access to '{file}' denied")
    return _real_open(file, *args, **kwargs)


builtins.open = _safe_open

# Patch ``io.open`` -- used by some libraries directly.
_real_io_open = io.open  # type: ignore[attr-defined]


def _safe_io_open(file, *args, **kwargs):  # noqa: ANN001, ANN201
    if _is_blocked(file):
        raise PermissionError(f"sandbox: access to '{file}' denied")
    return _real_io_open(file, *args, **kwargs)


io.open = _safe_io_open  # type: ignore[assignment]

# Patch ``os.open`` -- low-level descriptor open.
_real_os_open = os.open


def _safe_os_open(path, *args, **kwargs):  # noqa: ANN001, ANN201
    if _is_blocked(path):
        raise PermissionError(f"sandbox: access to '{path}' denied")
    return _real_os_open(path, *args, **kwargs)


os.open = _safe_os_open

# ---------- run the user's script ---------- #

sys.argv = [str(SCRIPT)] + sys.argv[3:]
try:
    src = SCRIPT.read_text(encoding="utf-8", errors="replace")
except OSError as exc:  # noqa: BLE001
    sys.stderr.write(f"sandbox_shim: cannot read {SCRIPT}: {exc}\n")
    sys.exit(2)

ns: dict = {
    "__name__": "__main__",
    "__file__": str(SCRIPT),
    "__builtins__": builtins.__dict__,
    "__loader__": None,
    "__package__": None,
}
try:
    exec(compile(src, str(SCRIPT), "exec"), ns)  # noqa: S102
except SystemExit:
    raise
except BaseException:  # noqa: BLE001
    # Let unhandled exceptions surface in the bot's log just like a regular
    # Python script would. We re-raise so the parent process records a
    # non-zero exit code.
    raise
