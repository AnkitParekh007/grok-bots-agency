"""Scoped authorization for UNATTENDED approvals in synthetic evaluations.

The normal approval policy is unchanged: a human (authenticated in local mode) approves each exact plan.
Unattended approval exists only for the offline evaluation harness and is enforced here, in the runtime:
  - agent config `unattended_eval_approval.enabled` must be true (default false), AND
  - a signed-off authorization file must be supplied, with:
      kind == "synthetic-eval-unattended-approval", approval_ref, approver_identity, expires_at (ISO, UTC),
      stories (allowlist), environment, skills, actions, max_runs, model_classes, data_class == "synthetic"
  - the run's story / environment / model class / every plan skill+action must be inside the allowlist,
  - uses are counted atomically per approval_ref and may not exceed max_runs,
  - the first plan of a model class must have been approved MANUALLY by an authenticated approver
    (always for LIVE MODEL, regardless of the file).
Every decision is audited as tool `plan.approve_unattended`.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

KIND = "synthetic-eval-unattended-approval"
REQUIRED = ("approval_ref", "approver_identity", "expires_at", "stories", "environment", "skills", "actions", "max_runs",
            "model_classes", "data_class")


class AuthorizationError(Exception):
    pass


def model_class(label: str | None) -> str:
    return (label or "").split(" (")[0].strip()


def load_authorization(path) -> dict:
    try:
        a = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise AuthorizationError(f"authorization file unreadable: {e}")
    if a.get("kind") != KIND:
        raise AuthorizationError(f"authorization kind must be {KIND!r}")
    missing = [k for k in REQUIRED if a.get(k) in (None, "", [])]
    if missing:
        raise AuthorizationError(f"authorization missing fields: {missing}")
    if a["data_class"] != "synthetic":
        raise AuthorizationError("unattended approval is only allowed for data_class 'synthetic'")
    if not isinstance(a["max_runs"], int) or a["max_runs"] < 1:
        raise AuthorizationError("max_runs must be a positive integer")
    return a


def check(auth: dict, run: dict, env: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    exp = datetime.fromisoformat(auth["expires_at"].replace("Z", "+00:00"))
    if exp.tzinfo is None:
        raise AuthorizationError("expires_at must include a timezone")
    if now >= exp:
        raise AuthorizationError(f"authorization {auth['approval_ref']} expired at {auth['expires_at']}")
    if run.get("story_key") not in auth["stories"]:
        raise AuthorizationError(f"story {run.get('story_key')} not in authorization allowlist")
    if run.get("environment") != auth["environment"]:
        raise AuthorizationError(f"environment {run.get('environment')} not authorized")
    if env.get("production") is not False or env.get("data_class") != "synthetic":
        raise AuthorizationError("environment is not a non-production synthetic environment")
    mc = model_class(run.get("model_label"))
    if mc not in auth["model_classes"]:
        raise AuthorizationError(f"model class {mc!r} not authorized (allowed {auth['model_classes']})")
    for t in (run.get("plan_json") or {}).get("test_cases", []):
        if t.get("skill") not in auth["skills"] or t.get("action") not in auth["actions"]:
            raise AuthorizationError(f"test {t.get('id')} uses {t.get('skill')}.{t.get('action')} outside the authorization")
