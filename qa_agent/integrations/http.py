"""Read-only HTTP client shared by the Jira and Bitbucket adapters.

Guarantees: GET only (any other method raises before a socket is opened), host allowlist, https for any
non-loopback host, credentials only from env vars and never in logs/errors/audit, per-request timeouts,
bounded retries (GET only; 429/502/503/504 and connection errors; Retry-After honoured and capped; never on
401/403/404/other 4xx), every request audited through the ToolGateway hash-chained audit log."""
from __future__ import annotations
import base64, os, time, math, threading, ssl, uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urljoin
import httpx
from ..environments import _is_loopback
from ..util import redact, register_secret_env, register_secret_value
from ..jobs import JobConflict

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
    if u.username or u.password or u.query or u.fragment:
        raise IntegrationNotAuthorized(f"{name}: base_url must not contain credentials/query/fragment")
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
    if not (cfg.get("projects") or cfg.get("repositories") or cfg.get("workspaces") or
            (cfg.get("discovery_only") is True and cfg.get("discovery_approved") is True)):
        raise IntegrationNotAuthorized(f"{name}: an explicit project/repository allowlist is required")


def _cred_envs(cfg):
    a = cfg.get("auth") or {}
    return [v for k, v in a.items() if k.endswith("_env") and v]


class ReadOnlyHttpClient:
    _cooldowns = {}
    _cooldown_lock = threading.Lock()

    def __init__(self, name: str, cfg: dict, gateway=None, run_id=None, transport=None, sleep=time.sleep, clock=time.time):
        check_authorization(name, cfg)
        self.name, self.cfg, self.gateway, self.run_id, self.sleep = name, cfg, gateway, run_id, sleep
        self.clock = clock
        self.base = cfg["base_url"].rstrip("/") + "/"
        self.allowed_hosts = list(cfg["allowed_hosts"])
        self.timeout = float(cfg.get("timeout_s", 15))
        self.max_retries = max(0, min(int(cfg.get("max_retries", 2)), 5))
        self.backoff = float(cfg.get("backoff_base_s", 0.5))
        self.max_backoff = float(cfg.get("max_backoff_s", 10))
        self.max_bytes = int(cfg.get("max_response_bytes", 2_000_000))
        if not 0 < self.max_bytes <= 20_000_000 or not 0 < self.timeout <= 600:
            raise IntegrationNotAuthorized(f"{name}: invalid response/timeout bounds")
        self.cooldown_scope = cfg.get("rate_limit_scope") or (name + ":" + self.base)
        for k in _cred_envs(cfg):
            register_secret_env(k)
        verify = ssl.create_default_context(cafile=cfg["tls_ca_file"]) if cfg.get("tls_ca_file") else True
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=False, transport=transport,
                                    trust_env=False, verify=verify)
        if self.gateway:
            self.gateway.store.x("CREATE TABLE IF NOT EXISTS integration_cooldowns(scope TEXT PRIMARY KEY, blocked_until REAL)")

    def _cooldown_until(self):
        if self.gateway:
            rows = self.gateway.store.q("SELECT blocked_until FROM integration_cooldowns WHERE scope=?", (self.cooldown_scope,))
            return rows[0]["blocked_until"] if rows else 0
        with self._cooldown_lock:
            return self._cooldowns.get(self.cooldown_scope, 0)

    def _remember_cooldown(self, seconds):
        if seconds <= 0:
            return
        until = self.clock() + seconds
        if self.gateway:
            self.gateway.store.x("INSERT INTO integration_cooldowns(scope,blocked_until) VALUES(?,?) "
                                 "ON CONFLICT(scope) DO UPDATE SET blocked_until=MAX(blocked_until,excluded.blocked_until)",
                                 (self.cooldown_scope, until))
        else:
            with self._cooldown_lock:
                self._cooldowns[self.cooldown_scope] = max(until, self._cooldowns.get(self.cooldown_scope, 0))

    def _retry_after(self, value):
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self.clock()
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0, seconds) if math.isfinite(seconds) else None

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
        url = path if path.startswith(("http://", "https://")) else urljoin(self.base, path.lstrip("/"))
        return str(httpx.URL(url))  # Normalize dot segments before testing the approved API prefix.

    def _check_url(self, url):
        u, b = urlparse(url), urlparse(self.base)
        if u.username or u.password or u.fragment or u.hostname != b.hostname or u.hostname not in self.allowed_hosts or u.scheme != b.scheme or u.port != b.port:
            raise IntegrationNotAuthorized(f"{self.name}: refusing request to non-allowlisted origin {u.scheme}://{u.hostname}")

        base_path = b.path.rstrip("/")
        if base_path and u.path != base_path and not u.path.startswith(base_path + "/"):
            raise IntegrationNotAuthorized(f"{self.name}: request escapes approved API base path")

    def request(self, method: str, path: str, params=None, accept="application/json", *, deadline=None):
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
        ceiling = time.monotonic() + self.timeout * (self.max_retries + 1) + self.max_backoff * self.max_retries
        if deadline is not None and (not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline)):
            raise IntegrationError("invalid source request deadline")
        deadline = min(ceiling, deadline) if deadline is not None else ceiling
        while True:
            if self.gateway:
                self.gateway.store.assert_job_lease(self.run_id)
                denial = self.gateway.audit_delivery.admission_denial(self.gateway.policy.get("audit_delivery"))
                if denial:
                    raise IntegrationError(denial)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IntegrationError(f"{self.name}: total request deadline exceeded")
            if self._cooldown_until() > self.clock():
                raise IntegrationError(f"{self.name}: upstream cooldown active; retry later", 429)
            t = time.time()
            request_id = uuid.uuid4().hex
            self._audit(method, path, "allow", "durable source request intent", "pending", t, attempt,
                        tool="integration.http.intent", request_id=request_id)
            try:
                if self.gateway:
                    self.gateway.store.assert_job_lease(self.run_id)
                with self._client.stream(method, url, params=params, timeout=min(self.timeout, remaining),
                                         headers={**self._auth_headers(), "Accept": accept, "Accept-Encoding": "identity"}) as response:
                    retry_after = response.headers.get("retry-after")
                    if response.status_code >= 300:
                        # Error/redirect bodies are never required or buffered.
                        r = httpx.Response(response.status_code, headers=response.headers, request=response.request)
                    else:
                        encoding = response.headers.get("content-encoding", "identity").lower()
                        if encoding not in ("", "identity"):
                            raise IntegrationError(f"{self.name}: compressed integration responses are not permitted")
                        try:
                            declared = int(response.headers.get("content-length", "0"))
                        except ValueError:
                            raise IntegrationError(f"{self.name}: invalid content length") from None
                        if declared < 0 or declared > self.max_bytes:
                            raise IntegrationError(f"{self.name}: response exceeds max_response_bytes")
                        body = bytearray()
                        chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                        for chunk in chunks:
                            if self.gateway:
                                self.gateway.store.assert_job_lease(self.run_id)
                            if time.monotonic() > deadline:
                                raise IntegrationError(f"{self.name}: total streaming deadline exceeded")
                            if len(body) + len(chunk) > self.max_bytes:
                                raise IntegrationError(f"{self.name}: response exceeds max_response_bytes")
                            body.extend(chunk)
                        r = httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self._audit(method, path, "allow", "policy ok", f"error: {type(e).__name__}", t, attempt, request_id=request_id)
                if attempt < self.max_retries:
                    attempt += 1; self._pause_retry(self._delay(attempt, None), deadline); continue
                raise IntegrationError(f"{self.name}: {type(e).__name__} after {attempt + 1} attempt(s)") from None
            except (JobConflict, IntegrationError) as exc:
                self._audit(method, path, "allow", "source request stopped by fence/response limits", f"error: {type(exc).__name__}", t, attempt, request_id=request_id)
                raise
            self._audit(method, path, "allow", "policy ok", f"http {r.status_code}", t, attempt, request_id=request_id)
            cooldown = self._retry_after(retry_after) if r.status_code in RETRY_STATUS else None
            if cooldown is not None:
                self._remember_cooldown(cooldown)
                if cooldown > self.max_backoff:
                    raise IntegrationError(f"{self.name}: upstream cooldown exceeds inline wait; retry later", r.status_code)
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                attempt += 1; self._pause_retry(self._delay(attempt, retry_after), deadline); continue
            if r.status_code in (401, 403):
                raise IntegrationNotAuthorized(f"{self.name}: upstream returned {r.status_code} (not retried)", r.status_code)
            if r.status_code == 404:
                raise IntegrationError(f"{self.name}: not found: {path}", 404)
            if r.status_code >= 300:
                raise IntegrationError(f"{self.name}: upstream returned {r.status_code}", r.status_code)
            if len(r.content) > self.max_bytes:
                raise IntegrationError(f"{self.name}: response exceeds max_response_bytes")
            if self.gateway:
                self.gateway.store.assert_job_lease(self.run_id)
            return r

    def get_json(self, path, params=None, *, deadline=None):
        response = self.request("GET", path, params, deadline=deadline)
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise IntegrationError(f"{self.name}: invalid JSON response") from None

    def get_text(self, path, params=None):
        return self.request("GET", path, params, accept="*/*").text

    def _delay(self, attempt, retry_after):
        seconds = self._retry_after(retry_after)
        if seconds is not None:
            return min(seconds, self.max_backoff)
        return min(self.backoff * (2 ** (attempt - 1)), self.max_backoff)

    def _pause_retry(self, seconds, deadline):
        if seconds >= deadline - time.monotonic():
            raise IntegrationError(f"{self.name}: total request retry deadline exceeded; retry later")
        self.sleep(seconds)

    def _audit(self, method, path, decision, reason, outcome="not_executed", t=None, attempt=0, *, tool="integration.http", request_id=None):
        if not self.gateway:
            return
        clean = urlparse(self.url(path))
        args = {"integration": self.name, "method": method, "host": clean.hostname, "path": clean.path, "attempt": attempt}
        if request_id:
            args["request_id"] = request_id
        self.gateway.record(self.run_id or self.gateway.store.job_run_id(), tool, args, decision, reason, outcome)

    # forbid mutation helpers outright
    def post(self, *a, **k): raise ReadOnlyViolation(f"{self.name}: POST is not permitted (read-only integration)")
    put = patch = delete = post


