"""Read-only HTTP client shared by the Jira and Bitbucket adapters.

Guarantees: GET only (any other method raises before a socket is opened), host allowlist, https for any
non-loopback host, credentials only from env vars and never in logs/errors/audit, per-request timeouts,
bounded retries (GET only; 429/502/503/504 and connection errors; Retry-After honoured and capped; never on
401/403/404/other 4xx), every request audited through the ToolGateway hash-chained audit log."""
from __future__ import annotations
import base64, os, time
from urllib.parse import urlparse, urljoin
import httpx
from ..environments import _is_loopback
from ..util import redact, register_secret_env, register_secret_value

RETRY_STATUS = {429, 502, 503, 504}
READ_METHODS = {"GET"}


class IntegrationError(Exception):
    """Safe-to-display error (already redacted)."""
    def __init__(self, msg, status=None):
        super().__init__(redact(msg))
        self.status = status


class IntegrationNotAuthorized(IntegrationError):
    pass


class ReadOnlyViolation(IntegrationError):
    pass


class Unsupported(IntegrationError):
    pass


def check_authorization(name: str, cfg: dict) -> None:
    """Fail closed unless the adapter is explicitly enabled, approved, credentialed and allowlisted."""
    if cfg.get("enabled") is not True:
        raise IntegrationNotAuthorized(f"{name}: disabled (enabled=false). Real integrations require explicit enablement.")
    if cfg.get("approved") is not True or not cfg.get("approval_ref"):
        raise IntegrationNotAuthorized(f"{name}: not approved (approved=true and approval_ref required)")
    base = cfg.get("base_url") or ""
    u = urlparse(base)
    host = u.hostname or ""
    if not host:
        raise IntegrationNotAuthorized(f"{name}: base_url not configured")
    if host not in (cfg.get("allowed_hosts") or []):
        raise IntegrationNotAuthorized(f"{name}: host {host} not in allowed_hosts")
    if u.scheme != "https":
        if not (u.scheme == "http" and _is_loopback(host) and cfg.get("local_mock") is True):
            raise IntegrationNotAuthorized(f"{name}: https required (plain http only for loopback local_mock servers)")
    if cfg.get("local_mock") is True and not _is_loopback(host):
        raise IntegrationNotAuthorized(f"{name}: local_mock=true is only valid for loopback hosts")
    for k in _cred_envs(cfg):
        if not os.environ.get(k):
            raise IntegrationNotAuthorized(f"{name}: credential env var {k} is not set")
    if not (cfg.get("projects") or cfg.get("repositories") or cfg.get("workspaces")):
        raise IntegrationNotAuthorized(f"{name}: an explicit project/repository allowlist is required")


def _cred_envs(cfg):
    a = cfg.get("auth") or {}
    return [v for k, v in a.items() if k.endswith("_env") and v]


class ReadOnlyHttpClient:
    def __init__(self, name: str, cfg: dict, gateway=None, run_id=None, transport=None, sleep=time.sleep):
        check_authorization(name, cfg)
        self.name, self.cfg, self.gateway, self.run_id, self.sleep = name, cfg, gateway, run_id, sleep
        self.base = cfg["base_url"].rstrip("/") + "/"
        self.allowed_hosts = list(cfg["allowed_hosts"])
        self.timeout = float(cfg.get("timeout_s", 15))
        self.max_retries = max(0, min(int(cfg.get("max_retries", 2)), 5))
        self.backoff = float(cfg.get("backoff_base_s", 0.5))
        self.max_backoff = float(cfg.get("max_backoff_s", 10))
        self.max_bytes = int(cfg.get("max_response_bytes", 2_000_000))
        for k in _cred_envs(cfg):
            register_secret_env(k)
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=False, transport=transport)

    def _auth_headers(self):
        a = self.cfg.get("auth") or {}
        kind = a.get("type")
        if kind == "bearer":
            return {"Authorization": "Bearer " + os.environ[a["token_env"]]}
        if kind == "basic":
            raw = f"{os.environ[a['user_env']]}:{os.environ[a['token_env']]}".encode()
            tok = base64.b64encode(raw).decode()
            register_secret_value(tok)
            return {"Authorization": "Basic " + tok}
        raise IntegrationNotAuthorized(f"{self.name}: auth.type must be bearer|basic")

    def url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else urljoin(self.base, path.lstrip("/"))

    def _check_url(self, url):
        u, b = urlparse(url), urlparse(self.base)
        if u.hostname not in self.allowed_hosts or u.scheme != b.scheme or u.port != b.port:
            raise IntegrationNotAuthorized(f"{self.name}: refusing request to non-allowlisted origin {u.scheme}://{u.hostname}")

    def request(self, method: str, path: str, params=None, accept="application/json"):
        method = (method or "").upper()
        if method not in READ_METHODS:
            self._audit(method, path, "deny", "read-only integration: method not allowed")
            raise ReadOnlyViolation(f"{self.name}: {method} is not permitted (read-only integration)")
        url = self.url(path)
        if self.gateway:   # the default-deny policy must also allow integration.http with this method
            reason = self.gateway.check(self.run_id, "integration.http", {"method": method}, None)
            if reason:
                self._audit(method, path, "deny", reason)
                raise ReadOnlyViolation(f"{self.name}: policy denied: {reason}")
        try:
            self._check_url(url)
        except IntegrationNotAuthorized as e:
            self._audit(method, path, "deny", str(e))
            raise
        attempt = 0
        while True:
            t = time.time()
            try:
                r = self._client.request(method, url, params=params, headers={**self._auth_headers(), "Accept": accept})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self._audit(method, path, "allow", "policy ok", f"error: {type(e).__name__}", t, attempt)
                if attempt < self.max_retries:
                    attempt += 1; self.sleep(self._delay(attempt, None)); continue
                raise IntegrationError(f"{self.name}: {type(e).__name__} after {attempt + 1} attempt(s)") from None
            self._audit(method, path, "allow", "policy ok", f"http {r.status_code}", t, attempt)
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                attempt += 1; self.sleep(self._delay(attempt, r.headers.get("retry-after"))); continue
            if r.status_code in (401, 403):
                raise IntegrationNotAuthorized(f"{self.name}: upstream returned {r.status_code} (not retried)", r.status_code)
            if r.status_code == 404:
                raise IntegrationError(f"{self.name}: not found: {path}", 404)
            if r.status_code >= 300:
                raise IntegrationError(f"{self.name}: upstream returned {r.status_code}", r.status_code)
            if len(r.content) > self.max_bytes:
                raise IntegrationError(f"{self.name}: response exceeds max_response_bytes")
            return r

    def get_json(self, path, params=None):
        return self.request("GET", path, params).json()

    def get_text(self, path, params=None):
        return self.request("GET", path, params, accept="*/*").text

    def _delay(self, attempt, retry_after):
        try:
            if retry_after is not None:
                return min(float(retry_after), self.max_backoff)
        except ValueError:
            pass
        return min(self.backoff * (2 ** (attempt - 1)), self.max_backoff)

    def _audit(self, method, path, decision, reason, outcome="not_executed", t=None, attempt=0):
        if not self.gateway:
            return
        clean = urlparse(self.url(path))
        args = {"integration": self.name, "method": method, "host": clean.hostname, "path": clean.path, "attempt": attempt}
        self.gateway.record(self.run_id, "integration.http", args, decision, reason, outcome)

    # forbid mutation helpers outright
    def post(self, *a, **k): raise ReadOnlyViolation(f"{self.name}: POST is not permitted (read-only integration)")
    put = patch = delete = post


