import ipaddress, os
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


class RegistryEnvironmentProvider(EnvironmentProvider):
    """Only registered, non-production, loopback environments resolve in Milestone A."""

    def __init__(self, cfg: dict):
        self.envs = cfg.get("environments", {})

    def resolve(self, name):
        e = self.envs.get(name)
        if not e:
            raise EnvironmentRefused(f"environment '{name}' is not registered")
        e = dict(e, name=name)
        if e.get("base_url_env_override") and os.environ.get(e["base_url_env_override"]):
            e["base_url"] = os.environ[e["base_url_env_override"]]
        host = urlparse(e["base_url"]).hostname or ""
        if e.get("production") is not False:
            raise EnvironmentRefused("production environments are not permitted")
        if not _is_loopback(host) or host not in e.get("allowed_hosts", []):
            raise EnvironmentRefused(f"host {host} not permitted in Milestone A (loopback only)")
        return e

    def is_url_allowed(self, env, url):
        u = urlparse(url)
        base = urlparse(env["base_url"])
        return (u.scheme in ("http", "https") and _is_loopback(u.hostname or "")
                and u.hostname in env.get("allowed_hosts", []) and u.port == base.port)
