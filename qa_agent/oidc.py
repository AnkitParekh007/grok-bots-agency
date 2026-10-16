"""OIDC authorization-code/PKCE and fixed-issuer JWT verification .

All URLs are administrator-pinned; no URL is learned from a token header. No raw
token or PKCE verifier is logged. Client sessions/authorization are separate layers.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.fernet import Fernet


class OidcError(ValueError):
    pass


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _url(value):
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise OidcError("OIDC URLs must be explicit HTTPS endpoints without credentials/query/fragment")
    return value


class OidcClient:
    def __init__(self, cfg, store, *, transport=None, clock=time.time):
        cfg = {"max_auth_age_s": 3600, **cfg}
        if type(cfg["max_auth_age_s"]) is not int or not 60 <= cfg["max_auth_age_s"] <= 86400:
            raise OidcError("invalid OIDC authentication age limit")
        self.cfg, self.store, self.clock = cfg, store, clock
        for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "redirect_uri"):
            _url(cfg[key])
        if not cfg.get("client_id") or not cfg.get("approval_ref"):
            raise OidcError("approved OIDC issuer and client are required")
        allowed = set(cfg.get("allowed_hosts") or [])
        if any(urlsplit(cfg[k]).hostname not in allowed for k in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")):
            raise OidcError("OIDC endpoint host is not explicitly approved")
        self.algorithms = cfg.get("algorithms", ["RS256"])
        if not self.algorithms or any(a not in ("RS256", "PS256", "ES256", "EdDSA") for a in self.algorithms):
            raise OidcError("OIDC requires an explicitly approved asymmetric algorithm")
        key = os.environ.get(cfg.get("flow_key_env", "QA_OIDC_FLOW_KEY"))
        if not key:
            raise OidcError("OIDC flow encryption key is required")
        try:
            self.fernet = Fernet(key.encode())
        except (ValueError, TypeError):
            raise OidcError("invalid OIDC flow encryption key") from None
        self.http = httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=10)
        self.lock = threading.RLock()
        self.keys, self.keys_at, self.last_refresh = {}, 0, 0
        store.x("CREATE TABLE IF NOT EXISTS oidc_flows(state_hash TEXT PRIMARY KEY, binding_hash TEXT, "
                "nonce_hash TEXT, verifier TEXT, expires REAL)")
        store.x("CREATE TABLE IF NOT EXISTS oidc_logout_replays(jti_hash TEXT PRIMARY KEY, expires REAL)")

    def _request(self, method, endpoint, **kwargs):
        try:
            with self.http.stream(method, endpoint, **kwargs) as r:
                if r.status_code != 200 or r.headers.get("content-encoding", "identity") not in ("identity", ""):
                    raise OidcError("OIDC endpoint rejected request")
                body = bytearray()
                deadline = time.monotonic() + 15
                chunks = (r.content,) if r.is_stream_consumed else r.iter_raw()
                for chunk in chunks:
                    if time.monotonic() > deadline:
                        raise OidcError("OIDC response deadline exceeded")
                    if len(body) + len(chunk) > 1_000_000:
                        raise OidcError("OIDC response exceeds size bound")
                    body.extend(chunk)
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise OidcError("invalid OIDC response")
                return result
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise OidcError("OIDC request or response validation failed") from None

    def begin(self):
        state, binding, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.store.x("DELETE FROM oidc_flows WHERE expires<?", (self.clock(),))
        self.store.x("INSERT INTO oidc_flows VALUES(?,?,?,?,?)", (token_hash(state), token_hash(binding),
                     token_hash(nonce), self.fernet.encrypt(verifier.encode()).decode(), self.clock() + 300))
        query = {"response_type": "code", "client_id": self.cfg["client_id"], "redirect_uri": self.cfg["redirect_uri"],
                 "scope": "openid profile", "state": state, "nonce": nonce, "code_challenge": challenge,
                 "code_challenge_method": "S256", "max_age": self.cfg["max_auth_age_s"]}
        if self.cfg.get("acr_values"):
            query["acr_values"] = " ".join(self.cfg["acr_values"])
        return {"url": self.cfg["authorization_endpoint"] + "?" + urlencode(query), "binding": binding}

    def complete(self, code, state, binding):
        if not all(isinstance(v, str) and 1 <= len(v) <= 2048 for v in (code, state, binding)):
            raise OidcError("invalid OIDC callback")
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                r = self.store.db.execute("SELECT * FROM oidc_flows WHERE state_hash=?", (token_hash(state),)).fetchone()
                if not r or r["expires"] <= self.clock() or not hmac.compare_digest(r["binding_hash"], token_hash(binding)):
                    raise OidcError("expired, replayed or unbound OIDC callback")
                self.store.db.execute("DELETE FROM oidc_flows WHERE state_hash=?", (token_hash(state),))
                self.store.db.commit()
            except Exception:
                self.store.db.rollback()
                raise
        verifier = self.fernet.decrypt(r["verifier"].encode()).decode()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.cfg["redirect_uri"],
                "client_id": self.cfg["client_id"], "code_verifier": verifier}
        secret_env = self.cfg.get("client_secret_env")
        auth = None
        if secret_env:
            secret = os.environ.get(secret_env)
            if not secret:
                raise OidcError("OIDC client credential unavailable")
            auth = (self.cfg["client_id"], secret)
        response = self._request("POST", self.cfg["token_endpoint"], data=data, auth=auth,
                                 headers={"Accept-Encoding": "identity"})
        token = response.get("id_token")
        claims = self.verify(token, expected_nonce_hash=r["nonce_hash"])
        return claims

    def _key(self, kid):
        with self.lock:
            now = self.clock()
            if kid not in self.keys or now - self.keys_at > 300:
                if self.keys and now - self.last_refresh < 10:
                    if kid not in self.keys:
                        raise OidcError("unknown OIDC signing key")
                else:
                    self.last_refresh = now
                    result = self._request("GET", self.cfg["jwks_uri"], headers={"Accept-Encoding": "identity"})
                    raw_keys = result.get("keys")
                    if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 32:
                        raise OidcError("invalid OIDC key set")
                    parsed = {}
                    for key in raw_keys:
                        if not isinstance(key, dict):
                            raise OidcError("invalid OIDC key entry")
                        if key.get("use", "sig") != "sig" or (key.get("alg") and key["alg"] not in self.algorithms) or key.get("kty") == "oct":
                            continue
                        if key.get("kid") in parsed or not isinstance(key.get("kid"), str):
                            raise OidcError("ambiguous OIDC signing key")
                        parsed[key["kid"]] = key
                    self.keys, self.keys_at = parsed, now
            if kid not in self.keys:
                raise OidcError("unknown OIDC signing key")
            return self.keys[kid]

    def verify(self, token, *, expected_nonce_hash=None, logout=False):
        try:
            if not isinstance(token, str) or len(token) > 32768:
                raise OidcError("invalid OIDC token size")
            header = jwt.get_unverified_header(token)
            kid, alg = header.get("kid"), header.get("alg")
            if alg not in self.algorithms or not isinstance(kid, str) or not 1 <= len(kid) <= 128 \
                    or any(k in header for k in ("jku", "jwk", "x5u", "crit")):
                raise OidcError("unapproved OIDC token header")
            raw_key = self._key(kid)
            if raw_key.get("alg") and raw_key["alg"] != alg:
                raise OidcError("OIDC key algorithm mismatch")
            key = jwt.PyJWK.from_dict(raw_key, algorithm=alg)
            if alg in ("RS256", "PS256") and key.key.key_size < 2048:
                raise OidcError("OIDC RSA key is too small")
            if alg == "ES256" and key.key.curve.name != "secp256r1":
                raise OidcError("OIDC EC curve does not match algorithm")
            required = ["iss", "aud", "iat"] + (["jti", "events"] if logout else ["exp", "sub", "nonce", "auth_time"])
            claims = jwt.decode(token, key.key, algorithms=self.algorithms, audience=self.cfg["client_id"],
                                issuer=self.cfg["issuer"], leeway=5, options={"require": required})
            if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != self.cfg["client_id"]:
                raise OidcError("OIDC authorized party mismatch")
            if claims.get("azp") and claims["azp"] != self.cfg["client_id"]:
                raise OidcError("OIDC authorized party mismatch")
            if logout:
                event = "http://schemas.openid.net/event/backchannel-logout"
                if not isinstance(claims["events"], dict) or event not in claims["events"] \
                        or not isinstance(claims["jti"], str) or len(claims["jti"]) > 256 \
                        or "nonce" in claims or self.clock() - claims["iat"] > 120 \
                        or not (claims.get("sid") or claims.get("sub")):
                    raise OidcError("invalid OIDC logout event")
                return claims
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 256:
                raise OidcError("invalid OIDC subject")
            age = self.clock() - claims["auth_time"]
            if age < -5 or age > self.cfg["max_auth_age_s"] + 5:
                raise OidcError("OIDC authentication is too old")
            if not expected_nonce_hash or not isinstance(claims["nonce"], str) \
                    or not hmac.compare_digest(token_hash(claims["nonce"]), expected_nonce_hash):
                raise OidcError("OIDC nonce mismatch")
            required_amr = self.cfg.get("required_amr", [])
            if required_amr and not set(required_amr).intersection(claims.get("amr", [])):
                raise OidcError("OIDC authentication strength requirement not met")
            required_acr = self.cfg.get("acr_values", [])
            if required_acr and claims.get("acr") not in required_acr:
                raise OidcError("OIDC authentication context requirement not met")
            return claims
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise OidcError("OIDC token validation failed") from None

    def consume_logout(self, token):
        claims = self.verify(token, logout=True)
        hashed = token_hash(claims["jti"])
        with self.store.lock:
            self.store.db.execute("DELETE FROM oidc_logout_replays WHERE expires<?", (self.clock(),))
            try:
                self.store.db.execute("INSERT INTO oidc_logout_replays VALUES(?,?)", (hashed, self.clock() + 300))
            except Exception:
                raise OidcError("replayed OIDC logout event") from None
        return claims

    def close(self):
        self.http.close()
