"""Opaque revocable sessions. """
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class SessionManager:
    def __init__(self, store, users=None, *, key=None, shared=False, idle_s=900, lifetime_s=3600,
                 max_per_identity=20, clock=time.time):
        if shared and not key:
            raise ValueError("shared deployment requires session encryption key")
        if not 60 <= idle_s <= 3600 or not idle_s <= lifetime_s <= 86400:
            raise ValueError("invalid bounded session lifetimes")
        self.store, self.users, self.clock = store, users, clock
        self.idle_s, self.lifetime_s, self.cap = idle_s, lifetime_s, max_per_identity
        self.fernet = Fernet(key.encode() if isinstance(key, str) else key) if key else None
        store.x("CREATE TABLE IF NOT EXISTS web_sessions(token_hash TEXT PRIMARY KEY, csrf_hash TEXT, "
                "identity TEXT, issuer TEXT, subject TEXT, external_sid TEXT, payload TEXT, "
                "expires REAL, last_seen REAL, revoked REAL)")
        store.x("CREATE INDEX IF NOT EXISTS session_identity ON web_sessions(identity,expires)")

    def revision(self, identity):
        if self.users is None:
            return None
        user = self.users._load().get("users", {}).get(identity)
        if not user or user.get("disabled"):
            return None
        if user.get("expires_at"):
            from datetime import datetime
            if datetime.fromisoformat(user["expires_at"]).timestamp() <= self.clock():
                return None
        return _hash(json.dumps(user, sort_keys=True, separators=(",", ":")))

    def issue(self, principal, *, issuer=None, subject=None, sid=None, expires_at=None):
        now = self.clock()
        expiry = min(now + self.lifetime_s, expires_at if expires_at is not None else now + self.lifetime_s)
        if expiry <= now:
            raise ValueError("cannot issue an expired session")
        if not principal.get("name") or principal.get("role") not in ("viewer", "requester", "approver") \
                or principal.get("mode") not in ("local", "oidc", "service"):
            raise ValueError("invalid session principal")
        revision = self.revision(principal["name"]) if principal.get("mode") == "local" else None
        if principal.get("mode") == "local" and not revision:
            raise ValueError("local session identity is unavailable")
        if principal.get("mode") == "local":
            credential_expiry = self.users._load()["users"][principal["name"]].get("expires_at")
            if credential_expiry:
                from datetime import datetime
                expiry = min(expiry, datetime.fromisoformat(credential_expiry).timestamp())
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        binding = {"token_hash": _hash(token), "csrf_hash": _hash(csrf), "identity": principal["name"],
                   "issuer": issuer, "subject": subject, "external_sid": sid, "expires": expiry}
        data = json.dumps({"principal": principal, "revision": revision, "expires": expiry, "binding": binding}, separators=(",", ":"))
        sealed = self.fernet.encrypt(data.encode()).decode() if self.fernet else data
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute("DELETE FROM web_sessions WHERE expires<?", (now,))
                n = self.store.db.execute("SELECT COUNT(*) FROM web_sessions WHERE identity=? AND revoked IS NULL AND expires>?",
                                          (principal["name"], now)).fetchone()[0]
                if n >= self.cap:
                    raise ValueError("active session limit reached")
                self.store.db.execute("INSERT INTO web_sessions VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                                      (_hash(token), _hash(csrf), principal["name"], issuer, subject, sid, sealed, expiry, now))
                self.store.db.commit()
            except Exception:
                self.store.db.rollback()
                raise
        return {"token": token, "csrf": csrf, "expires": expiry}

    def verify(self, token):
        if not isinstance(token, str) or len(token) != 43:
            return None
        return self.verify_reference(_hash(token), touch=True)

    def verify_reference(self, token_hash, *, touch=False):
        """Internal job authorization check; no raw session token is persisted in jobs.

        This method is never an HTTP authentication endpoint. Background work must
        not extend a human session's idle lifetime merely by checking its reference.
        """
        if not isinstance(token_hash, str) or len(token_hash) != 64 or any(c not in "0123456789abcdef" for c in token_hash):
            return None
        now = self.clock()
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM web_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if not row or row["revoked"] is not None or row["expires"] <= now or row["last_seen"] + self.idle_s <= now:
                return None
            try:
                body = self.fernet.decrypt(row["payload"].encode()).decode() if self.fernet else row["payload"]
                payload = json.loads(body)
            except (InvalidToken, ValueError, UnicodeError, AttributeError):
                return None
            if not isinstance(payload, dict) or payload.get("expires") != row["expires"]:
                return None
            binding = payload.get("binding")
            expected_keys = {"token_hash", "csrf_hash", "identity", "issuer", "subject", "external_sid", "expires"}
            if not isinstance(binding, dict) or set(binding) != expected_keys or any(value != row[name] for name, value in binding.items()):
                return None
            principal = payload.get("principal")
            if not isinstance(principal, dict) or principal.get("name") != row["identity"]:
                return None
            if principal.get("mode") == "local" and self.revision(principal["name"]) != payload.get("revision"):
                return None
            changed = self.store.db.execute("UPDATE web_sessions SET last_seen=? WHERE token_hash=? AND revoked IS NULL",
                                            (now, token_hash)).rowcount if touch else 1
            return principal if changed == 1 else None

    def csrf_ok(self, token, header, cookie):
        if not all(isinstance(v, str) and len(v) == 43 for v in (token, header, cookie)):
            return False
        if not hmac.compare_digest(header, cookie) or self.verify(token) is None:
            return False
        row = self.store.q("SELECT csrf_hash FROM web_sessions WHERE token_hash=? AND revoked IS NULL", (_hash(token),))
        return bool(row and hmac.compare_digest(row[0]["csrf_hash"], _hash(header)))

    def revoke(self, token):
        if isinstance(token, str) and len(token) == 43:
            self.store.x("UPDATE web_sessions SET revoked=? WHERE token_hash=? AND revoked IS NULL", (self.clock(), _hash(token)))

    def revoke_external(self, issuer, *, subject=None, sid=None):
        if not issuer or not (subject or sid):
            raise ValueError("logout requires issuer and subject or external session id")
        sql, value = ("UPDATE web_sessions SET revoked=? WHERE issuer=? AND external_sid=? AND revoked IS NULL", sid) if sid else ("UPDATE web_sessions SET revoked=? WHERE issuer=? AND subject=? AND revoked IS NULL", subject)
        self.store.x(sql,
                     (self.clock(), issuer, value))
