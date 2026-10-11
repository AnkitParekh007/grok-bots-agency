import hashlib, json, time
from datetime import datetime, timezone

def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)

def sha256(data) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------- secret redaction ----------------
import os as _os, re as _re

_SECRET_ENV_NAMES = {"XAI_API_KEY"}
_SECRET_PATTERNS = [
    _re.compile(r"xai-[A-Za-z0-9_\-]{8,}"),
    _re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    _re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
]


def register_secret_env(name: str):
    """Register an env var whose *value* must never appear in logs, events, reports or API responses."""
    if name:
        _SECRET_ENV_NAMES.add(name)


def redact(text):
    if text is None:
        return None
    s = str(text)
    for n in _SECRET_ENV_NAMES:
        v = _os.environ.get(n)
        if v and len(v) >= 6:
            s = s.replace(v, "[REDACTED]")
    for p in _SECRET_PATTERNS:
        s = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", s)
    return s
