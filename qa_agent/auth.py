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
    mode: str          # "local" (authenticated) | "demo" (unauthenticated)

    @property
    def authenticated(self):
        return self.mode == "local" and self.name is not None


def _hash(secret, salt):
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), ITER).hex()


class UserStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self):
        if not self.path.exists():
            return {"users": {}}
        return json.loads(self.path.read_text())

    def add(self, name, role):
        if role not in ROLES:
            raise ValueError(f"role must be one of {list(ROLES)}")
        if not name.isidentifier():
            raise ValueError("username must be alphanumeric/underscore")
        d = self._load()
        secret, salt = secrets.token_urlsafe(24), secrets.token_hex(16)
        d["users"][name] = {"role": role, "salt": salt, "token_hash": _hash(secret, salt)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2))
        os.chmod(self.path, 0o600)
        return f"{name}.{secret}"

    def verify(self, token):
        if not token or "." not in token:
            return None
        name, secret = token.split(".", 1)
        u = self._load()["users"].get(name)
        if not u or not hmac.compare_digest(_hash(secret, u["salt"]), u["token_hash"]):
            return None
        return Principal(name=name, role=u["role"], mode="local")


def has_role(p: Principal, role: str) -> bool:
    return ROLES.get(p.role, 0) >= ROLES[role]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user")
    a.add_argument("name")
    a.add_argument("--role", required=True, choices=list(ROLES))
    a.add_argument("--users-file", default="data/users.json")
    args = ap.parse_args()
    tok = UserStore(Path(args.users_file)).add(args.name, args.role)
    print(f"Created {args.name} ({args.role}). Token (shown once, store it safely):\n{tok}")


if __name__ == "__main__":
    main()
