"""Auto-install runtime dependencies for hosted bot uploads.

For Python, we either honour a sibling ``requirements.txt`` if present, or
parse ``import`` / ``from … import`` statements from the source file and
``pip install`` the non-stdlib top-level packages we don't already have.

For Node.js, we ``npm install`` if a sibling ``package.json`` exists.

For PHP, we ``composer install`` if a sibling ``composer.json`` exists.

The work is done in a thread-pool to keep the event loop responsive; output
is captured and returned so it can be logged or surfaced to the user.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

logger = logging.getLogger(__name__)


# ---- Python helpers ---- #

_STDLIB_MODULES: frozenset[str] | None = None


def _stdlib_module_names() -> frozenset[str]:
    global _STDLIB_MODULES
    if _STDLIB_MODULES is None:
        names: set[str] = set(getattr(sys, "stdlib_module_names", set()))
        # Builtin modules are never on PyPI either.
        names.update(sys.builtin_module_names)
        _STDLIB_MODULES = frozenset(names)
    return _STDLIB_MODULES


# Map of common import names whose PyPI distribution name differs.
PYPI_NAME_MAP: dict[str, str] = {
    "telegram": "python-telegram-bot",
    "telebot": "pyTelegramBotAPI",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "google": "google-api-python-client",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
    "nacl": "PyNaCl",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "discord": "discord.py",
}


_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([a-zA-Z_][\w.]*)\s+import\s+|import\s+([a-zA-Z_][\w., ]*))",
    re.MULTILINE,
)


def parse_python_imports(source: str) -> list[str]:
    """Return a sorted list of top-level imported module names."""
    found: set[str] = set()
    for from_mod, import_mods in _IMPORT_RE.findall(source):
        if from_mod:
            found.add(from_mod.split(".")[0])
            continue
        for chunk in import_mods.split(","):
            name = chunk.strip().split(" as ")[0].strip()
            if name:
                found.add(name.split(".")[0])
    return sorted(found)


def _is_python_module_available(mod: str) -> bool:
    """Quick heuristic for already-installed importable modules."""
    paths = sysconfig.get_paths()
    site = Path(paths["purelib"])
    if (site / mod).is_dir() or (site / f"{mod}.py").is_file():
        return True
    if (site / f"{mod}.pyd").is_file():
        return True
    # Fall back to a real import attempt for tricky packages (e.g. extension
    # modules in non-standard locations). We swallow ImportError because the
    # user code will surface the real error if launch fails.
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


def python_pip_targets(source: str) -> list[str]:
    """Compute the list of pip-install targets for a Python script."""
    stdlib = _stdlib_module_names()
    pkgs: list[str] = []
    for mod in parse_python_imports(source):
        if mod in stdlib:
            continue
        if _is_python_module_available(mod):
            continue
        pkgs.append(PYPI_NAME_MAP.get(mod, mod))
    return pkgs


# ---- Async installer ---- #

async def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await proc.communicate()
    return proc.returncode or 0, out_b.decode("utf-8", "replace")


async def install_dependencies(*, language: str, file_path: str) -> tuple[bool, str]:
    """Install dependencies for a hosted bot. Returns (ok, log)."""
    file = Path(file_path)
    cwd = file.parent
    log_lines: list[str] = []
    try:
        if language == "python":
            req = cwd / "requirements.txt"
            if req.exists():
                py = sys.executable or "python"
                rc, out = await _run([py, "-m", "pip", "install", "--no-input",
                                       "-r", str(req)], cwd)
                log_lines.append(f"$ pip install -r requirements.txt\n{out}")
                return (rc == 0), "\n".join(log_lines)
            try:
                src = file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return False, f"read failed: {exc}"
            targets = python_pip_targets(src)
            if not targets:
                return True, "no extra deps"
            py = sys.executable or "python"
            rc, out = await _run(
                [py, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
                 *targets], cwd,
            )
            log_lines.append(f"$ pip install {' '.join(targets)}\n{out}")
            return (rc == 0), "\n".join(log_lines)

        if language == "node":
            pkg = cwd / "package.json"
            if not pkg.exists():
                return True, "no package.json"
            npm = shutil.which("npm")
            if not npm:
                return False, "npm not installed"
            rc, out = await _run([npm, "install", "--no-fund", "--no-audit"], cwd)
            log_lines.append(f"$ npm install\n{out}")
            return (rc == 0), "\n".join(log_lines)

        if language == "php":
            comp_json = cwd / "composer.json"
            if not comp_json.exists():
                return True, "no composer.json"
            composer = shutil.which("composer")
            if not composer:
                return True, "composer not installed (skipping)"
            rc, out = await _run(
                [composer, "install", "--no-interaction", "--no-progress"], cwd,
            )
            log_lines.append(f"$ composer install\n{out}")
            return (rc == 0), "\n".join(log_lines)
    except Exception as exc:  # noqa: BLE001
        return False, f"installer crashed: {exc}"

    return True, ""


async def install_missing_python_module(module: str, *, cwd: Path) -> tuple[bool, str]:
    """Install a single Python package by import name (with PYPI rename map)."""
    pkg = PYPI_NAME_MAP.get(module, module)
    py = sys.executable or "python"
    rc, out = await _run(
        [py, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", pkg],
        cwd,
    )
    return (rc == 0), f"$ pip install {pkg}\n{out}"


async def install_missing_node_module(module: str, *, cwd: Path) -> tuple[bool, str]:
    """Install a single Node.js package via ``npm install``."""
    npm = shutil.which("npm")
    if not npm:
        return False, "npm not installed"
    rc, out = await _run(
        [npm, "install", "--no-fund", "--no-audit", module], cwd,
    )
    return (rc == 0), f"$ npm install {module}\n{out}"


def _ignored() -> None:  # placeholder for any future sync helpers
    return None


# Silence the unused import warning when subprocess is referenced but only
# via asyncio.create_subprocess_exec at runtime.
_ = subprocess
