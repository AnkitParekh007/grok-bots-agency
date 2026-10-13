"""Server-side evaluation grants for UNATTENDED approvals in synthetic evaluations.

Replaces the earlier self-declared JSON authorization file (removed: it carried no verifiable provenance).

A grant is created only by an AUTHENTICATED approver (local auth: hashed bearer token -> Principal). The creator identity
comes from the auth session; any identity fields supplied in the request are ignored. Grants are stored in SQLite and
bound to an eval scope (stories, environment, skills, actions, model classes), with an expiry and a use limit.
Integrity: each row carries grant_hash = sha256(canonical scope fields) and its creation is recorded in the hash-chained
audit log with that hash; a grant whose row hash does not match, or that has no matching audited creation, is rejected
(edited / forged rows). Limits: someone with write access to the DB *and* the ability to rebuild the audit chain can still
forge a grant; see docs/b2/CONTAMINATION_AND_INDEPENDENCE.md and the hash-chain trust limits in README.
Use counting is atomic (single UPDATE ... WHERE uses < max_uses AND revoked_at IS NULL). Revocation is immediate.
The first plan of a model class must have been approved MANUALLY by an authenticated approver (always for LIVE MODEL).
Normal (per-plan human) approval is unchanged. Every decision is audited.
"""
import json, uuid
from datetime import datetime, timezone
from .auth import has_role
from .util import canon, sha256, now

SCOPE_FIELDS = ("stories", "environment", "skills", "actions", "model_classes", "expires_at", "max_uses",
                "require_first_manual_approval", "purpose")


class AuthorizationError(Exception):
    pass


def model_class(label: str | None) -> str:
    return (label or "").split(" (")[0].strip()


def _grant_hash(g):
    return sha256(canon({"grant_id": g["grant_id"], "created_by": g["created_by"], "created_at": g["created_at"],
                         **{k: g[k] for k in SCOPE_FIELDS}}))


def create_grant(store, gateway, principal, spec: dict) -> dict:
    if principal is None or not principal.authenticated or principal.mode != "local":
        gateway.record(None, "eval.grant_create", {"user": getattr(principal, "name", None)}, "deny", "authenticated local approver required")
        raise AuthorizationError("eval grants require an authenticated approver (local auth mode)")
    if not has_role(principal, "approver"):
        gateway.record(None, "eval.grant_create", {"user": principal.name}, "deny", f"role {principal.role} < approver")
        raise AuthorizationError("only approvers can create eval grants")
    def lst(k):
        v = spec.get(k)
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            raise AuthorizationError(f"grant field {k!r} must be a non-empty list of strings")
        return sorted(set(v))
    exp = spec.get("expires_at")
    try:
        e = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        assert e.tzinfo is not None
    except Exception:
        raise AuthorizationError("expires_at must be an ISO-8601 timestamp with timezone")
    if e <= datetime.now(timezone.utc):
        raise AuthorizationError("expires_at must be in the future")
    mu = spec.get("max_uses")
    if not isinstance(mu, int) or isinstance(mu, bool) or mu < 1:
        raise AuthorizationError("max_uses must be a positive integer")
    if not isinstance(spec.get("environment"), str) or not spec["environment"]:
        raise AuthorizationError("environment required")
    g = {"grant_id": "egr-" + uuid.uuid4().hex[:16], "created_by": principal.name,     # identity from the session only
         "created_at": now(), "stories": lst("stories"), "environment": spec["environment"], "skills": lst("skills"),
         "actions": lst("actions"), "model_classes": lst("model_classes"), "expires_at": e.isoformat(), "max_uses": mu,
         "require_first_manual_approval": spec.get("require_first_manual_approval", True) is not False,
         "purpose": "synthetic-eval"}
    g["grant_hash"] = _grant_hash(g)
    store.insert_grant(g)
    gateway.record(None, "eval.grant_create", {"grant_id": g["grant_id"], "grant_hash": g["grant_hash"], "user": principal.name}, "allow",
                   "eval grant created by authenticated approver")
    return g


def revoke_grant(store, gateway, principal, grant_id):
    if principal is None or not principal.authenticated or not has_role(principal, "approver"):
        gateway.record(None, "eval.grant_revoke", {"grant_id": grant_id, "user": getattr(principal, "name", None)}, "deny", "approver required")
        raise AuthorizationError("only authenticated approvers can revoke eval grants")
    if not store.revoke_grant(grant_id, principal.name):
        raise AuthorizationError(f"grant {grant_id} not found or already revoked")
    gateway.record(None, "eval.grant_revoke", {"grant_id": grant_id, "user": principal.name}, "allow", "eval grant revoked")


def load_grant(store, grant_id) -> dict:
    g = store.get_grant(grant_id)
    if not g:
        raise AuthorizationError(f"grant {grant_id} not found")
    if _grant_hash(g) != g["grant_hash"]:
        raise AuthorizationError(f"grant {grant_id} integrity check failed (row edited)")
    created = [r for r in store.q("SELECT args_summary, decision FROM audit WHERE tool='eval.grant_create' AND decision='allow'")
               if g["grant_hash"] in (r["args_summary"] or "") and grant_id in (r["args_summary"] or "")]
    if not created:
        raise AuthorizationError(f"grant {grant_id} has no audited creation by an authenticated approver (forged)")
    if not store.verify_audit_chain()["ok"]:
        raise AuthorizationError("audit chain verification failed; refusing to trust grants")
    if g["revoked_at"]:
        raise AuthorizationError(f"grant {grant_id} revoked at {g['revoked_at']} by {g['revoked_by']}")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(g["expires_at"]):
        raise AuthorizationError(f"grant {grant_id} expired at {g['expires_at']}")
    return g


def check(g: dict, run: dict, env: dict) -> None:
    if run.get("story_key") not in g["stories"]:
        raise AuthorizationError(f"story {run.get('story_key')} not in grant scope")
    if run.get("environment") != g["environment"]:
        raise AuthorizationError(f"environment {run.get('environment')} not in grant scope")
    if env.get("production") is not False or env.get("data_class") != "synthetic":
        raise AuthorizationError("environment is not a non-production synthetic environment")
    mc = model_class(run.get("model_label"))
    if mc not in g["model_classes"]:
        raise AuthorizationError(f"model class {mc!r} not in grant scope")
    for t in (run.get("plan_json") or {}).get("test_cases", []):
        if t.get("skill") not in g["skills"] or t.get("action") not in g["actions"]:
            raise AuthorizationError(f"test {t.get('id')} uses {t.get('skill')}.{t.get('action')} outside the grant scope")


def main(argv=None):
    """CLI with local auth (token from env QA_TOKEN; never pass tokens as arguments):
      python -m qa_agent.eval_authorization create --data-dir D --users-file U --spec spec.json
      python -m qa_agent.eval_authorization revoke --data-dir D --users-file U --grant egr-...
      python -m qa_agent.eval_authorization list   --data-dir D --users-file U
      python -m qa_agent.eval_authorization review --data-dir D --users-file U --run RUN --plan-hash H --decision approved|rejected
    """
    import argparse, os, sys
    from pathlib import Path
    from .auth import UserStore
    from .runtime import QAAgent
    from . import ROOT
    from .util import load_json
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["create", "revoke", "list", "review"])
    ap.add_argument("--data-dir", required=True); ap.add_argument("--users-file", required=True)
    ap.add_argument("--spec"); ap.add_argument("--grant"); ap.add_argument("--run"); ap.add_argument("--plan-hash")
    ap.add_argument("--decision", choices=["approved", "rejected"]); ap.add_argument("--comment", default="")
    a = ap.parse_args(argv)
    p = UserStore(Path(a.users_file)).verify(os.environ.get("QA_TOKEN", ""))
    if not p:
        print("authentication failed (set QA_TOKEN to your local approver token)", file=sys.stderr); return 2
    cfg = load_json(ROOT / "config/agent.json"); cfg["model"]["provider"] = "replay"   # review/grants never call a model
    agent = QAAgent(Path(a.data_dir), agent_cfg=cfg)
    try:
        if a.cmd == "create":
            spec = json.loads(Path(a.spec).read_text())
            out = create_grant(agent.store, agent.gateway, p, spec)
        elif a.cmd == "revoke":
            revoke_grant(agent.store, agent.gateway, p, a.grant); out = {"revoked": a.grant}
        elif a.cmd == "list":
            if not has_role(p, "approver"):
                raise AuthorizationError("approver required")
            out = agent.store.q("SELECT * FROM eval_grants ORDER BY created_at")
        else:
            out = agent.review_plan(a.run, p, a.decision, a.plan_hash, a.comment)
    except (AuthorizationError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr); return 1
    print(json.dumps(out, indent=2, default=str)); return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
