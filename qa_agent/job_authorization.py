"""Server-created caller receipts for durable work; no bearer/session secrets stored."""
from dataclasses import asdict

from .auth import Principal, has_role
from .util import sha256


class JobAuthorization:
    def __init__(self, agent, sessions, users, mode, *, forbid_self_approval=True):
        self.agent, self.sessions, self.users, self.mode = agent, sessions, users, mode
        self.forbid_self_approval = forbid_self_approval

    def receipt(self, request, principal, cookie_name):
        if self.mode == "demo":
            return {"mode": "demo", "principal": asdict(principal)}
        if request.headers.get("authorization", "").lower().startswith("bearer "):
            return {"mode": "local_credential", "principal": asdict(principal),
                    "revision": self.sessions.revision(principal.name)}
        token = request.cookies.get(cookie_name)
        if not isinstance(token, str) or len(token) != 43:
            raise ValueError("queued work requires a live workspace session")
        return {"mode": "session", "reference": sha256(token), "principal": asdict(principal)}

    def _principal(self, receipt):
        if not isinstance(receipt, dict):
            return None
        expected = receipt.get("principal")
        if receipt.get("mode") == "session":
            current = self.sessions.verify_reference(receipt.get("reference"))
        elif receipt.get("mode") == "local_credential" and self.mode == "local" and isinstance(expected, dict):
            name = expected.get("name")
            if not receipt.get("revision") or self.sessions.revision(name) != receipt["revision"]:
                return None
            user = self.users._load().get("users", {}).get(name, {})
            current = asdict(Principal(name=name, role=user.get("role"), mode="local", tenant_id=user.get("tenant_id", "local"),
                                      project_roles=user.get("project_roles"), display_name=name, kind=user.get("kind", "human")))
        else:
            return None
        if not current or current != expected:
            return None
        p = Principal(**current)
        if p.mode == "oidc" and p.authorization_revision != self.agent.access.revision:
            return None
        if p.mode != self.mode or not p.authenticated:
            return None
        return p

    def check(self, receipt, run, kind):
        if receipt.get("mode") == "operator":
            return True  # Only trusted in-process callers can create this receipt.
        if receipt.get("mode") == "demo":
            return self.mode == "demo" and not self.agent.shared
        p = self._principal(receipt)
        role = "requester" if kind == "planning" else "approver"
        if not p or not has_role(p, role) or p.tenant_id != run.get("tenant_id", "local"):
            return False
        project = run.get("project")
        if project and not self.agent.access.allows(p, project, role):
            return False
        if not project and (self.agent.access.strict or p.project_roles is not None):
            return False
        if kind == "planning":
            return p.name == run.get("requested_by")
        approval = self.agent.approvals.latest(run["id"])
        return bool(p.kind == "human" and approval and approval.get("identity") == p.name
                    and self.agent.approvals.is_approved(run["id"], run.get("plan_hash"))
                    and (not self.forbid_self_approval or p.name != run.get("requested_by")))

    def __call__(self, job):
        run = self.agent.store.get_run(job["run_id"])
        return bool(run and self.check(job["payload"]["authorization"], run, job["kind"]))
