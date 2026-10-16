"""Environment registry. Fail-closed rules (C0):
- production is always refused (production flag missing/true, 'prod' in the name, or host matching a prohibited pattern);
- loopback synthetic environments (status verified-synthetic) resolve as before;
- any other (non-loopback) environment is refused unless ALL of: status == "verified", a safety_verification record
  (verified_by, verified_at, approval_ref), https base_url whose host is on allowed_hosts, >=1 synthetic test account
  (credential referenced by env var name, never stored), and a cleanup procedure (procedure_ref + steps).
Dev and Playground ship 'unverified' with no base_url, so they cannot execute."""
import fnmatch, ipaddress, os, re
from urllib.parse import urlparse
from .interfaces import EnvironmentProvider


class EnvironmentRefused(Exception):
    pass


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_PROD_NAME = re.compile(r"(?i)(^|[-_.])(prod|production|live)([-_.]|$)")


class RegistryEnvironmentProvider(EnvironmentProvider):
    def __init__(self, cfg: dict):
        self.envs = cfg.get("environments", {})
        self.prohibited = list(cfg.get("prohibited_host_patterns") or [])

    def _base(self, name):
        e = self.envs.get(name)
        if not e:
            raise EnvironmentRefused(f"environment '{name}' is not registered")
        e = dict(e, name=name)
        if e.get("base_url_env_override") and os.environ.get(e["base_url_env_override"]):
            e["base_url"] = os.environ[e["base_url_env_override"]]
        return e

    def problems(self, e) -> list[str]:
        """All reasons an environment may not execute (empty list => executable)."""
        p = []
        if e.get("production") is not False or _PROD_NAME.search(e["name"]) or e.get("kind") == "production":
            return ["production environments are prohibited"]
        host = urlparse(e.get("base_url") or "").hostname or ""
        if not host:
            return ([f"status is '{e.get('status', 'unverified')}' (explicit safety verification required)"]
                    if e.get("status") != "verified-synthetic" else []) + ["base_url not configured"]
        if any(fnmatch.fnmatch(host, pat) for pat in self.prohibited):
            return [f"host {host} matches a prohibited (production) host pattern"]
        if host not in (e.get("allowed_hosts") or []):
            p.append(f"host {host} not in allowed_hosts")
        if _is_loopback(host):
            if e.get("status") not in ("verified-synthetic",):
                p.append(f"status is '{e.get('status')}', not verified-synthetic")
            return p
        if e.get("status") != "verified":
            p.append(f"status is '{e.get('status', 'unverified')}' (explicit safety verification required)")
        if urlparse(e["base_url"]).scheme != "https":
            p.append("non-loopback environments must use https")
        sv = e.get("safety_verification") or {}
        if not all(sv.get(k) for k in ("verified_by", "verified_at", "approval_ref")):
            p.append("safety_verification record (verified_by, verified_at, approval_ref) missing")
        accts = e.get("test_accounts") or []
        if not accts or not all(a.get("synthetic") is True and a.get("credential_env") for a in accts):
            p.append("synthetic test accounts (synthetic=true, credential_env) required")
        elif not all(os.environ.get(a["credential_env"]) for a in accts):
            p.append("test account credential env var(s) not set")
        cl = e.get("cleanup") or {}
        if not cl.get("procedure_ref") or not cl.get("steps"):
            p.append("cleanup procedure (procedure_ref + steps) required")
        if e.get("data_class") != "synthetic":
            p.append("data_class must be synthetic")
        return p

    def resolve(self, name):
        e = self._base(name)
        probs = self.problems(e)
        if probs:
            raise EnvironmentRefused(f"environment '{name}' not executable: " + "; ".join(probs))
        return e

    def list_environments(self):
        out = []
        for n in self.envs:
            e = self._base(n)
            probs = self.problems(e)
            out.append({"name": n, "kind": e.get("kind", "synthetic"), "status": e.get("status", "unverified"),
                        "executable": not probs, "reasons": probs, "data_class": e.get("data_class")})
        return out

    def is_url_allowed(self, env, url):
        u, base = urlparse(url), urlparse(env["base_url"])
        if u.username or u.password or u.fragment or u.scheme not in ("http", "https"):
            return False
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        base_port = base.port or (443 if base.scheme == "https" else 80)
        if host not in env.get("allowed_hosts", []) or port != base_port or u.scheme != base.scheme:
            return False
        if any(fnmatch.fnmatch(host, pat) for pat in self.prohibited):
            return False
        if _is_loopback(host):
            return u.scheme in ("http", "https")
        return u.scheme == "https" and env.get("status") == "verified"
