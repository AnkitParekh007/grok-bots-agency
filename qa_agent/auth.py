"""Local auth boundary (NOT SSO). Users file holds PBKDF2-hashed bearer tokens and roles.

Token format: "<username>.<secret>" (secret shown once at creation). Roles: viewer < requester < approver.
CLI:  python -m qa_agent.auth add-user ankit --role approver [--users-file data/users.json]
"""
import argparse, hashlib, hmac, json, os, secrets
from dataclasses import dataclass
from pathlib import Path

ROLES = {"viewer": 1, "requester": 2, "approver": 3}
ITER = 60000


@dataclass
class Principal:
    name: str | None
    role: str
    mode: str          # local, oidc, service (authenticated), or demo
    tenant_id: str = "local"
    project_roles: dict | None = None
    display_name: str | None = None
    kind: str = "human"
    authorization_revision: str | None = None

    @property
    def authenticated(self):
        return self.mode in ("local", "oidc", "service") and self.name is not None


def _hash(secret, salt):
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), ITER).hex()


class UserStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self):
        if not self.path.exists():
            return {"users": {}}
        return json.loads(self.path.read_text())

    def add(self, name, role, *, tenant_id=None, project_roles=None, expires_at=None, kind=None):
        if role not in ROLES:
            raise ValueError(f"role must be one of {list(ROLES)}")
        if not name.isidentifier():
            raise ValueError("username must be alphanumeric/underscore")
        d = self._load()
        previous = d.get("users", {}).get(name, {})
        tenant_id = tenant_id or previous.get("tenant_id", "local")
        project_roles = previous.get("project_roles") if project_roles is None else project_roles
        kind = kind or previous.get("kind", "human")
        expires_at = expires_at if expires_at is not None else previous.get("expires_at")
        secret, salt = secrets.token_urlsafe(24), secrets.token_hex(16)
        if project_roles is not None and any(v not in ROLES for v in project_roles.values()):
            raise ValueError("invalid project role")
        if kind not in ("human", "service"):
            raise ValueError("identity kind must be human or service")
        if kind == "service" and not expires_at:
            raise ValueError("service credential expiry is required")
        if expires_at:
            from datetime import datetime, timezone
            if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                raise ValueError("credential expiry must be a future UTC timestamp")
        d["users"][name] = {"role": role, "salt": salt, "token_hash": _hash(secret, salt), "tenant_id": tenant_id,
                             "project_roles": project_roles, "expires_at": expires_at, "kind": kind}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2))
        os.chmod(self.path, 0o600)
        return f"{name}.{secret}"

    def verify(self, token):
        if not isinstance(token, str) or len(token) > 512 or "." not in token:
            return None
        name, secret = token.split(".", 1)
        u = self._load()["users"].get(name)
        if not u or u.get("disabled") or u.get("role") not in ROLES or u.get("kind", "human") not in ("human", "service"):
            return None
        try:
            if u.get("expires_at"):
                from datetime import datetime, timezone
                if datetime.fromisoformat(u["expires_at"]) <= datetime.now(timezone.utc):
                    return None
            if not hmac.compare_digest(_hash(secret, u["salt"]), u["token_hash"]):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return Principal(name=name, role=u["role"], mode="local", tenant_id=u.get("tenant_id", "local"),
                         project_roles=u.get("project_roles"), display_name=name, kind=u.get("kind", "human"))

    def disable(self, name):
        data = self._load()
        if name not in data.get("users", {}):
            raise ValueError("user not found")
        data["users"][name]["disabled"] = True
        self.path.write_text(json.dumps(data, indent=2))


def has_role(p: Principal, role: str) -> bool:
    return ROLES.get(p.role, 0) >= ROLES[role]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user")
    a.add_argument("name")
    a.add_argument("--role", required=True, choices=list(ROLES))
    a.add_argument("--users-file", default="data/users.json")
    a.add_argument("--tenant")
    a.add_argument("--project", action="append")
    a.add_argument("--expires-at")
    a.add_argument("--kind", choices=["human", "service"])
    d = sub.add_parser("disable-user")
    d.add_argument("name")
    d.add_argument("--users-file", default="data/users.json")
    args = ap.parse_args()
    if args.cmd == "disable-user":
        UserStore(Path(args.users_file)).disable(args.name)
        print(f"Disabled {args.name}; existing sessions will be rejected on their next request.")
        return
    tok = UserStore(Path(args.users_file)).add(args.name, args.role, tenant_id=args.tenant,
          project_roles={p: args.role for p in args.project} if args.project else None,
          expires_at=args.expires_at, kind=args.kind)
    print(f"Created {args.name} ({args.role}). Token (shown once, store it safely):\n{tok}")


if __name__ == "__main__":
    main()
