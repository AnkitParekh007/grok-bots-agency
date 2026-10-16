"""Content-addressed execution inputs. Approval binds the whole package, not just cases.

Snapshots are retained with the run. At dispatch, live configuration/source bytes are
checked again and snapshot bytes are loaded/verified once. The executor uses these
captured bytes, preventing a later fixture/oracle file edit from changing a result.
"""
import inspect
import json
from pathlib import Path

from . import ROOT, planning
from .util import canon, sha256

FORMAT = "qa-execution-envelope/1"


class PackageChanged(ValueError):
    pass


def bounded_read(path, limit=20_000_000):
    with Path(path).open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise PackageChanged("execution input exceeds package byte limit")
    return body


def digest(run):
    """Calculate reviewed envelope hash, also detecting edited stored plan/understanding."""
    envelope = dict(run.get("execution_envelope_json") or {})
    if envelope.get("format") != FORMAT:
        raise PackageChanged("legacy or missing execution envelope; create a new plan")
    envelope["test_plan_sha256"] = sha256(canon(run.get("plan_json") or {}))
    envelope["planning_sha256"] = sha256(canon(run.get("planning_json") or {}))
    envelope["story_sha256"] = sha256(canon(run.get("story_json") or {}))
    return sha256(canon(envelope))


def inputs(agent, run, env, story=None, repository=None):
    oracle = bounded_read(agent.oracle_path(run["story_key"]))
    fixtures = {}
    root = (ROOT / agent.cfg["fixture_data_dir"]).resolve()
    for name in agent.fixture_files():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path.name != name:
            raise PackageChanged("fixture path escapes configured data directory")
        fixtures[name] = bounded_read(path)
    if sum(map(len, fixtures.values())) > 20_000_000:
        raise PackageChanged("combined fixture bytes exceed execution package limit")
    code = {str(p.relative_to(ROOT)).replace("\\", "/"): sha256(p.read_bytes())
            for p in sorted((ROOT / "qa_agent").rglob("*.py"))}
    # Also cover registered extension modules outside this repository.
    for skill in agent.skills.skills.values():
        for action, (_, fn) in skill.actions.items():
            source = inspect.getsourcefile(fn)
            if not source:
                raise PackageChanged("registered action has no verifiable source artifact")
            code[f"skill:{skill.name}:{action}"] = sha256(bounded_read(source))
    manifest = {"format": FORMAT,
                "test_plan_sha256": sha256(canon(run["plan_json"])),
                "planning_sha256": sha256(canon(run["planning_json"])),
                "story_sha256": sha256(canon(story if story is not None else run["story_json"])),
                "repository_sha256": sha256(canon(repository if repository is not None else run.get("repo_context_json"))),
                "environment": env, "policy_sha256": sha256(canon(agent.policy)),
                "config_sha256": sha256(canon(agent.cfg)),
                "catalog_version": planning.catalog_version(agent.skills),
                "schema_sha256": sha256(canon(planning.PLAN_STORY_SCHEMA)),
                "model": {k: run.get(k) for k in ("model_provider", "model_id", "model_version")},
                "oracle_sha256": sha256(oracle),
                "fixtures": {name: sha256(body) for name, body in fixtures.items()},
                "code_sha256": code}
    return manifest, oracle, fixtures


def capture(agent, run_id, env):
    run = agent.store.get_run(run_id)
    manifest, oracle, fixtures = inputs(agent, run, env)
    agent.artifacts.write_bytes(run_id, "inputs/oracles.json", oracle)
    agent.artifacts.write_bytes(run_id, "inputs/config.json", canon(agent.cfg).encode())
    for name, body in fixtures.items():
        agent.artifacts.write_bytes(run_id, "inputs/fixtures/" + name, body)
    agent.artifacts.write_bytes(run_id, "inputs/envelope.json", canon(manifest).encode())
    approval_hash = sha256(canon(manifest))
    agent.store.update_run(run_id, execution_envelope_json=manifest, plan_hash=approval_hash)
    return approval_hash


def verify(agent, run, env, *, check_sources=True):
    if digest(run) != run["plan_hash"]:
        raise PackageChanged("reviewed execution envelope or stored plan changed")
    story, repo = run["story_json"], run.get("repo_context_json")
    if check_sources:
        story = agent._tool(run["id"], "tracker.get_issue", {"key": run["story_key"]},
                            lambda: agent.tracker.get_issue(run["story_key"]))
        if run.get("repository"):
            repo = agent._tool(run["id"], "git.repo_context", {"repo": run["repository"]},
                              lambda: agent.git.repo_context(run["repository"]))
    current, _, _ = inputs(agent, run, env, story=story, repository=repo)
    manifest = run["execution_envelope_json"]
    if current != manifest:
        changed = sorted(k for k in current if current[k] != manifest.get(k))
        raise PackageChanged("execution inputs changed since review: " + ", ".join(changed))
    snapshot = agent.artifacts.path_for(run["id"], "inputs/envelope.json")
    if json.loads(bounded_read(snapshot)) != manifest:
        raise PackageChanged("execution envelope snapshot changed")
    oracle = bounded_read(agent.artifacts.path_for(run["id"], "inputs/oracles.json"))
    if sha256(oracle) != manifest["oracle_sha256"]:
        raise PackageChanged("oracle snapshot changed")
    fixtures = {}
    for name, expected in manifest["fixtures"].items():
        body = bounded_read(agent.artifacts.path_for(run["id"], "inputs/fixtures/" + name))
        if sha256(body) != expected:
            raise PackageChanged("fixture snapshot changed")
        fixtures[name] = body
    return json.loads(oracle), fixtures


def review_configuration(data_dir, run_id, approval_hash):
    """Load a configuration only after binding it to the explicitly reviewed hash.

    Used by the review-only CLI; it never starts model inference or execution.
    A caller cannot replace its config snapshot under an existing approval token.
    """
    import sqlite3
    data_dir = Path(data_dir).resolve()
    with sqlite3.connect((data_dir / "qa_agent.db").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise PackageChanged("review run not found")
    run = dict(row)
    for key in ("execution_envelope_json", "plan_json", "planning_json", "story_json"):
        run[key] = json.loads(run[key]) if run.get(key) else None
    if not approval_hash or run["plan_hash"] != approval_hash or digest(run) != approval_hash:
        raise PackageChanged("stale or altered execution package")
    root = (data_dir / "runs").resolve()
    path = (root / run_id / "inputs/config.json").resolve()
    if not path.is_relative_to(root):
        raise PackageChanged("review path outside run storage")
    cfg = json.loads(bounded_read(path))
    if sha256(canon(cfg)) != run["execution_envelope_json"]["config_sha256"]:
        raise PackageChanged("review configuration snapshot changed")
    return cfg
