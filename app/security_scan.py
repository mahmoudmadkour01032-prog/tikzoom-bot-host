"""Heuristic + AST-based scanner for user-uploaded bot files.

Goal: refuse to host files that try to (1) read platform secrets, (2) write
outside their sandbox, (3) execute the system shell with platform paths,
(4) phone home with stolen tokens, or (5) hide any of the above behind
``getattr`` / ``__import__`` / base64 / marshal / zlib obfuscation.

The scanner is purely static — it never executes the file. It runs entirely
locally with no third-party API calls, but the design exposes an optional
``ai_review`` hook so the platform can plug in an LLM-based second opinion
later (e.g. Gemini / Claude / GPT) without changing any callers.
"""
from __future__ import annotations

import ast
import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# Sandbox / platform paths a malicious bot might try to reach.
_PLATFORM_PATH_PATTERNS = [
    re.compile(r"(?i)c:[\\/]+tikzoom"),
    re.compile(r"(?i)bots[_-]?storage"),
    re.compile(r"(?i)platform\.db"),
    re.compile(r"(?i)\.env(?![a-z])"),  # bare ".env" but not ".envoy"
    re.compile(r"(?i)tikzoom-bot-host"),
    re.compile(r"(?i)windows[\\/]+system32[\\/]+config"),
    re.compile(r"(?i)/etc/(?:passwd|shadow|hosts)"),
    re.compile(r"(?i)/proc/(?:self|\d+)/environ"),
    re.compile(r"(?i)appdata[\\/]+roaming"),
    re.compile(r"(?i)\.ssh[\\/]"),
    re.compile(r"(?i)id_rsa(?:\.pub)?"),
    re.compile(r"(?i)token_hash|token_encrypted|fernet[_-]?key|webhook[_-]?secret"),
]

# Suspicious URLs that look like exfil endpoints.
_EXFIL_URL_PATTERN = re.compile(
    r"(?i)https?://"
    r"(?:[a-z0-9-]+\.)*"
    r"(?:webhook\.site|requestbin|pipedream\.net|ngrok\.|"
    r"discord(?:app)?\.com/api/webhooks|telegram\.me/[a-z0-9_]+/[a-z0-9_]+|"
    r"transfer\.sh|filebin\.net|file\.io|0x0\.st|paste\.ee|pastebin\.com)"
)

# Module names that on their own raise the scanner's eyebrows.
_DANGEROUS_PYTHON_MODULES = {
    "ctypes",          # arbitrary native calls
    "socket",          # only when combined with raw IPs / port scans (handled separately)
    "winreg",
    "_winreg",
    "win32api",
    "win32con",
    "win32security",
    "win32process",
    "pywintypes",
    "paramiko",        # SSH client
    "fabric",
    "smbprotocol",
    "ftplib",          # raw FTP exfil
    "telnetlib",
    "psutil",          # process/file enumeration
}

# Built-in callables that absolutely should not be reachable in a hosted bot.
_FORBIDDEN_PYTHON_BUILTINS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "execfile",
}

# Attribute access chains that signal obfuscation (importlib, marshal, etc.).
_OBFUSCATION_ATTR_CHAINS = {
    ("importlib", "import_module"),
    ("importlib", "__import__"),
    ("marshal", "loads"),
    ("pickle", "loads"),
    ("dill", "loads"),
    ("base64", "b64decode"),
    ("zlib", "decompress"),
    ("codecs", "decode"),
}

# Node.js patterns.
_NODE_PATTERNS = [
    (re.compile(r"\brequire\s*\(\s*['\"](child_process|fs|os|net|tls|http|https|dgram|dns)['\"]\s*\)"),
     "uses sensitive Node module"),
    (re.compile(r"\bprocess\.env\.(?!BOT_TOKEN|PORT|WEBHOOK_URL|WEBHOOK_PATH|PLATFORM)[A-Z_][A-Z0-9_]*"),
     "reads non-bot env var"),
    (re.compile(r"\beval\s*\("), "calls eval()"),
    (re.compile(r"\bnew\s+Function\s*\("), "uses new Function() (eval-equivalent)"),
    (re.compile(r"\bBuffer\.from\s*\([^)]*['\"]\s*,\s*['\"]base64['\"]\s*\)"),
     "decodes base64 payload"),
]

# PHP patterns.
_PHP_PATTERNS = [
    (re.compile(r"\b(eval|assert|create_function)\s*\("), "calls eval-equivalent"),
    (re.compile(r"\b(system|exec|shell_exec|passthru|proc_open|popen|pcntl_exec)\s*\("),
     "executes shell command"),
    (re.compile(r"\b(file_get_contents|fopen|readfile|file)\s*\(\s*['\"][^'\"]*(\.\.|/etc/|c:\\|tikzoom)",
                re.IGNORECASE),
     "reads outside sandbox"),
    (re.compile(r"\bgetenv\s*\(\s*['\"](?!BOT_TOKEN|PORT|WEBHOOK_URL|WEBHOOK_PATH|PLATFORM)"),
     "reads non-bot env var"),
    (re.compile(r"\$_(SERVER|ENV)\b"), "accesses server superglobal"),
    (re.compile(r"\bbase64_decode\s*\("), "decodes base64 payload"),
]


@dataclass
class ScanResult:
    """Summary of a single static-analysis pass over an uploaded bot file."""

    safe: bool = True
    risks: list[str] = field(default_factory=list)
    severity: str = "ok"  # ok | warn | block

    def add(self, risk: str, *, severity: str = "block") -> None:
        self.risks.append(risk)
        # Severity ratchets up but never down.
        order = {"ok": 0, "warn": 1, "block": 2}
        if order[severity] > order[self.severity]:
            self.severity = severity
        if severity == "block":
            self.safe = False

    def merge(self, other: ScanResult) -> None:
        for r in other.risks:
            self.risks.append(r)
        order = {"ok": 0, "warn": 1, "block": 2}
        if order[other.severity] > order[self.severity]:
            self.severity = other.severity
        if not other.safe:
            self.safe = False

    def summary(self) -> str:
        if self.safe and not self.risks:
            return "ملف نظيف."
        lines = ["تم اكتشاف المخاطر الآتية في الملف:"]
        for r in self.risks[:20]:
            lines.append(f"  • {r}")
        if len(self.risks) > 20:
            lines.append(f"  • ... و{len(self.risks) - 20} نتيجة أخرى")
        return "\n".join(lines)


# -------------------- public API -------------------- #

def scan_file(path: str | Path, language: str) -> ScanResult:
    """Scan a single uploaded bot file. Best-effort: never raises on bad input."""
    p = Path(path)
    try:
        raw_bytes = p.read_bytes()
    except OSError as exc:
        result = ScanResult()
        result.add(f"تعذر قراءة الملف: {exc}", severity="block")
        return result
    # Refuse files that are too large to analyse safely.
    if len(raw_bytes) > 4 * 1024 * 1024:
        result = ScanResult()
        result.add("الملف أكبر من 4MB — تم رفضه", severity="block")
        return result
    try:
        text = raw_bytes.decode("utf-8", "replace")
    except Exception:
        text = raw_bytes.decode("latin-1", "replace")
    return scan_text(text, language)


def scan_text(text: str, language: str) -> ScanResult:
    """Run the same heuristic checks on an in-memory source string."""
    result = ScanResult()
    _scan_common(text, result)
    if language == "python":
        _scan_python(text, result)
    elif language == "node":
        _scan_node(text, result)
    elif language == "php":
        _scan_php(text, result)
    else:
        result.add(f"لغة غير مدعومة: {language}", severity="block")
    return result


# -------------------- common (any language) -------------------- #

def _scan_common(text: str, result: ScanResult) -> None:
    for pat in _PLATFORM_PATH_PATTERNS:
        m = pat.search(text)
        if m:
            result.add(
                f"يحاول الوصول لمسار خاص بالاستضافة: <code>{_short(m.group(0))}</code>",
                severity="block",
            )
    for m in _EXFIL_URL_PATTERN.finditer(text):
        result.add(
            f"يحتوي على رابط مشبوه (يحتمل أن يستخدم لتسريب بيانات): "
            f"<code>{_short(m.group(0))}</code>",
            severity="block",
        )
    # Suspicious base64 blobs longer than 200 chars (often hide payloads).
    for blob in re.finditer(r"['\"]([A-Za-z0-9+/=]{200,})['\"]", text):
        token = blob.group(1)
        try:
            decoded = base64.b64decode(token, validate=True)
        except Exception:
            continue
        try:
            decoded_text = decoded.decode("utf-8", "replace")
        except Exception:
            continue
        if any(p.search(decoded_text) for p in _PLATFORM_PATH_PATTERNS):
            result.add(
                "حمولة مشفّرة بـ base64 تكشف بعد فك التشفير محاولة الوصول لمسار حساس",
                severity="block",
            )
        elif _EXFIL_URL_PATTERN.search(decoded_text):
            result.add(
                "حمولة مشفّرة بـ base64 تحتوي على رابط مشبوه بعد فك التشفير",
                severity="block",
            )


# -------------------- python (AST) -------------------- #

def _scan_python(text: str, result: ScanResult) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        result.add(
            f"الملف يحتوي على خطأ نحوي ولم يجتز الفحص: line {exc.lineno}",
            severity="block",
        )
        return
    visitor = _PyVisitor(result)
    visitor.visit(tree)


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, result: ScanResult) -> None:
        self.result = result

    # Imports ------------------------------------------------------- #
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in _DANGEROUS_PYTHON_MODULES:
                self.result.add(
                    f"استيراد وحدة خطرة: <code>{alias.name}</code>",
                    severity="block",
                )
            if alias.name in {"app", "tikzoom_bot_host"} or alias.name.startswith(
                ("app.", "tikzoom_bot_host."),
            ):
                self.result.add(
                    f"يستورد مباشرةً من حزمة الاستضافة: <code>{alias.name}</code>",
                    severity="block",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        top = mod.split(".")[0]
        if top in _DANGEROUS_PYTHON_MODULES:
            self.result.add(
                f"استيراد من وحدة خطرة: <code>{mod}</code>",
                severity="block",
            )
        if top in {"app", "tikzoom_bot_host"}:
            self.result.add(
                f"يستورد من حزمة الاستضافة: <code>{mod}</code>",
                severity="block",
            )
        self.generic_visit(node)

    # Calls --------------------------------------------------------- #
    def visit_Call(self, node: ast.Call) -> None:
        name = _flat_name(node.func)
        if name in _FORBIDDEN_PYTHON_BUILTINS:
            self.result.add(
                f"استدعاء ممنوع: <code>{name}()</code>",
                severity="block",
            )
        # os.system / subprocess.run / Popen / ...
        if name in {
            "os.system", "os.popen", "os.execv", "os.execvp",
            "subprocess.run", "subprocess.call", "subprocess.check_call",
            "subprocess.check_output", "subprocess.Popen",
            "platform.platform", "platform.uname",
        }:
            # Inspect the first argument for platform paths.
            if node.args:
                arg_repr = ast.dump(node.args[0])
                if any(p.search(arg_repr) for p in _PLATFORM_PATH_PATTERNS):
                    self.result.add(
                        f"يستدعي <code>{name}()</code> بمعامل يشير لمسار حساس",
                        severity="block",
                    )
                else:
                    # Subprocesses are still suspicious — a bot doesn't need them.
                    self.result.add(
                        f"استدعاء أمر نظام: <code>{name}()</code>",
                        severity="block",
                    )
        # open(...) with an absolute platform path.
        if name == "open" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if any(p.search(arg.value) for p in _PLATFORM_PATH_PATTERNS):
                    self.result.add(
                        f"يفتح ملف داخل الاستضافة: <code>{_short(arg.value)}</code>",
                        severity="block",
                    )
                elif arg.value.startswith(("/", "C:\\", "c:\\", "C:/", "c:/")):
                    # absolute paths to system locations are at least suspicious
                    self.result.add(
                        f"يفتح ملف بمسار مطلق خارج مجلد البوت: <code>{_short(arg.value)}</code>",
                        severity="warn",
                    )
        # importlib.import_module(...) / __import__("...") / etc.
        for chain in _OBFUSCATION_ATTR_CHAINS:
            if name == ".".join(chain):
                self.result.add(
                    f"يستخدم طريقة استيراد ديناميكي قد تخفي كود ضار: "
                    f"<code>{name}()</code>",
                    severity="block",
                )
                break
        # os.environ — if used with anything other than BOT_TOKEN, we treat as exfil attempt.
        if name in {"os.environ.get", "os.getenv"} and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.upper() not in {
                    "BOT_TOKEN", "PORT", "WEBHOOK_URL", "WEBHOOK_PATH", "PLATFORM",
                }:
                    self.result.add(
                        f"يقرأ متغير بيئة حساس: <code>{arg.value}</code>",
                        severity="block",
                    )
            else:
                self.result.add(
                    "يقرأ متغير بيئة عبر تعبير ديناميكي (قد يخفي تسريب توكن)",
                    severity="block",
                )
        self.generic_visit(node)

    # Attribute access of os.environ / dict-style. ------------------ #
    def visit_Attribute(self, node: ast.Attribute) -> None:
        flat = _flat_name(node)
        if flat == "os.environ":
            self.result.add(
                "يصل لـ <code>os.environ</code> كاملاً (يستخرج كل أسرار الخادم)",
                severity="block",
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        flat = _flat_name(node.value)
        if flat == "os.environ":
            # os.environ["X"] — only allow whitelist.
            slc = node.slice
            value = None
            if isinstance(slc, ast.Constant) and isinstance(slc.value, str):
                value = slc.value
            elif (
                hasattr(ast, "Index")
                and isinstance(slc, ast.Index)  # type: ignore[attr-defined]
                and isinstance(slc.value, ast.Constant)  # type: ignore[attr-defined]
                and isinstance(slc.value.value, str)  # type: ignore[attr-defined]
            ):
                value = slc.value.value  # type: ignore[attr-defined]
            allowed = {"BOT_TOKEN", "PORT", "WEBHOOK_URL", "WEBHOOK_PATH", "PLATFORM"}
            if value is not None and value.upper() not in allowed:
                self.result.add(
                    f"يقرأ متغير بيئة حساس: <code>{value}</code>",
                    severity="block",
                )
            elif value is None:
                self.result.add(
                    "يقرأ متغير بيئة عبر تعبير ديناميكي",
                    severity="block",
                )
        self.generic_visit(node)


def _flat_name(node: ast.AST) -> str:
    """Return ``a.b.c`` for nested attribute access, or '' if too dynamic."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


# -------------------- node / php (regex) -------------------- #

def _scan_node(text: str, result: ScanResult) -> None:
    # Block any direct platform-path access (already handled by _scan_common but
    # we still want to flag suspicious patterns specific to Node).
    for pat, msg in _NODE_PATTERNS:
        for m in pat.finditer(text):
            result.add(f"Node.js: {msg} — <code>{_short(m.group(0))}</code>",
                       severity="block")


def _scan_php(text: str, result: ScanResult) -> None:
    for pat, msg in _PHP_PATTERNS:
        for m in pat.finditer(text):
            result.add(f"PHP: {msg} — <code>{_short(m.group(0))}</code>",
                       severity="block")


# -------------------- helpers -------------------- #

def _short(s: str, *, n: int = 60) -> str:
    s = s.replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


# -------------------- optional AI hook -------------------- #

async def ai_review(_text: str, _language: str) -> ScanResult | None:
    """Optional hook for an LLM second-opinion review.

    By default this is a no-op; if a future version of TikZoom plugs in
    Gemini/Claude/GPT, callers can wrap this and merge results into the
    heuristic ``ScanResult``.
    """
    return None
