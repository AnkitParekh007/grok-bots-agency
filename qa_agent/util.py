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
