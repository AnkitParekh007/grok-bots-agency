"""Operator-reviewed domain inputs; environment verification alone grants no skills.

Approvals live only in trusted server configuration. The proposal helper has no
network or write-to-SUT capability and cannot approve its own output.
"""
import inspect
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import ROOT
from .environments import _is_loopback
from .util import canon, sha256

FORMAT = "qa-domain-approval/1"
FIELDS = {"format", "id", "version", "enabled", "approved", "approved_by", "approval_ref",
          "expires_at", "oracle_author", "oracle_reviewer", "independence_ref", "inputs_sha256"}


class DomainNotApproved(ValueError):
    pass


def input_manifest(skills, oracle_path, fixture_root, *, scope, environment):
    """Describe exact local review inputs, without reading credentials or any source API."""
    from .execution_package import bounded_read
    fixtures, actions = {}, {}
    root = Path(fixture_root).resolve()
    paths = sorted(root.glob("*.csv"))
    if len(paths) > 256:
        raise DomainNotApproved("domain fixture count exceeds limit")
    total = 0
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved.name != path.name:
            raise DomainNotApproved("domain fixture escapes configured directory")
        body = bounded_read(resolved)
        total += len(body)
        if total > 20_000_000:
            raise DomainNotApproved("domain fixture bytes exceed limit")
        fixtures[path.name] = sha256(body)
    for skill in skills.skills.values():
        for action, (_, fn) in skill.actions.items():
            source = inspect.getsourcefile(fn)
            if not source or len(actions) >= 256:
                raise DomainNotApproved("domain actions lack bounded verifiable source artifacts")
            actions[f"{skill.name}.{action}"] = sha256(bounded_read(source))
    return {"format": "qa-domain-inputs/1", "scope": scope,
            "environment_sha256": sha256(canon(environment)),
            "catalog_sha256": sha256(canon(skills.list())), "action_sources": actions,
            "oracle_sha256": sha256(bounded_read(oracle_path)), "fixtures": fixtures}


def describe(agent, run, environment):
    return input_manifest(agent.skills, agent.oracle_path(run["story_key"]), ROOT / agent.cfg["fixture_data_dir"],
                          scope={k: run.get(k) for k in ("tenant_id", "project", "repository", "story_key", "environment")},
                          environment=environment)


def synthetic_harness(agent, run, environment, story):
    from .providers.mock_jira import MockJiraProvider
    from .providers.git_stub import SyntheticGitProvider
    tracker = agent.tracker_for(run.get("project"))
    if not isinstance(tracker, MockJiraProvider) or tracker.kind != "mock_jira":
        return False
    if run.get("repository"):
        git = agent.git_for(run.get("project"))
        if not isinstance(git, SyntheticGitProvider) or git.kind != "synthetic_git":
            return False
    return (environment.get("kind") == "synthetic" and environment.get("status") == "verified-synthetic"
            and environment.get("production") is False and environment.get("data_class") == "synthetic"
            and _is_loopback(urlparse(environment.get("base_url") or "").hostname or "")
            and "synthetic" in (story.get("fields", {}).get("labels") or []))


def authorize(agent, run, environment, story):
    if synthetic_harness(agent, run, environment, story):
        return {"format": FORMAT, "mode": "synthetic_harness"}
    pc = agent.projects.get(run.get("project"))
    if not pc or pc.get("tenant_id", "local") != run.get("tenant_id"):
        raise DomainNotApproved("organization execution requires a domain approval in the exact tenant/project")
    agent.select(run["project"], run["story_key"], run.get("repository"), run["environment"])
    records = pc.get("domain_capabilities", [])
    if not isinstance(records, list) or len(records) > 100:
        raise DomainNotApproved("domain_capabilities must be a bounded list")
    expected = sha256(canon(describe(agent, run, environment)))
    matches = []
    for record in records:
        if not isinstance(record, dict) or set(record) != FIELDS or record.get("format") != FORMAT:
            raise DomainNotApproved("domain approval has an unsupported or incomplete contract")
        if record["enabled"] is not True or record["approved"] is not True:
            continue
        text = ("id", "version", "approved_by", "approval_ref", "oracle_author", "oracle_reviewer", "independence_ref")
        if any(not isinstance(record[k], str) or not record[k].strip() or len(record[k]) > 256 for k in text):
            raise DomainNotApproved("domain approval requires named reviewers and an independence evidence reference")
        if record["oracle_author"].strip().casefold() == record["oracle_reviewer"].strip().casefold():
            raise DomainNotApproved("expected results require a distinct author and reviewer")
        if not isinstance(record["inputs_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["inputs_sha256"]):
            raise DomainNotApproved("domain approval requires the exact input digest")
        try:
            expires = datetime.fromisoformat(record["expires_at"])
            if expires.tzinfo is None:
                raise ValueError("timezone missing")
        except (ValueError, TypeError):
            raise DomainNotApproved("domain approval requires a timezone-aware expiry") from None
        if expires > datetime.now(timezone.utc) and record["inputs_sha256"] == expected:
            matches.append(record)
    if len(matches) != 1:
        raise DomainNotApproved("exactly one current reviewed domain approval must match the story, environment, repository and capability inputs")
    return {"format": FORMAT, "mode": "reviewed_domain", "approval": dict(matches[0])}
