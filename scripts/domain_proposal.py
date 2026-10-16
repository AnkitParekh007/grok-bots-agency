"""Produce an unapproved, local-only domain review proposal. No source/model/SUT I/O."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa_agent import ROOT
from qa_agent.domain import FORMAT, input_manifest
from qa_agent.environments import RegistryEnvironmentProvider
from qa_agent.execution_package import bounded_read
from qa_agent.skills import LocalSkillRegistry
from qa_agent.util import canon, sha256


def proposal(cfg, environment_cfg, *, project, story, repository, environment):
    pc = (cfg.get("projects") or {}).get(project)
    if not isinstance(pc, dict) or not story.startswith(str(pc.get("jira_project")) + "-"):
        raise ValueError("configured project and matching story key required")
    if environment not in pc.get("environments", []) or (repository and repository not in pc.get("repositories", [])):
        raise ValueError("environment/repository is outside the configured project")
    root = (ROOT / cfg["oracles_dir"]).resolve()
    oracle = (root / (story + ".expected.json")).resolve()
    if oracle.parent != root or not oracle.is_relative_to(root):
        raise ValueError("oracle path outside configured directory")
    env = RegistryEnvironmentProvider(environment_cfg)._base(environment)
    inputs = input_manifest(LocalSkillRegistry(), oracle, ROOT / cfg["fixture_data_dir"],
                            scope={"tenant_id": pc.get("tenant_id", "local"), "project": project,
                                   "repository": repository, "story_key": story, "environment": environment},
                            environment=env)
    return {"format": "qa-domain-proposal/1", "execution_eligible": False,
            "notice": "Review material only. Verify the actual domain, independent expected results and environment; generating a digest grants nothing.",
            "inputs": inputs, "approval": {"format": FORMAT, "id": None, "version": None,
                "enabled": False, "approved": False, "approved_by": None, "approval_ref": None,
                "expires_at": None, "oracle_author": None, "oracle_reviewer": None,
                "independence_ref": None, "inputs_sha256": sha256(canon(inputs))}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/agent.json")
    parser.add_argument("--environments", type=Path, default=ROOT / "config/environments.json")
    parser.add_argument("--project", required=True)
    parser.add_argument("--story", required=True)
    parser.add_argument("--repository")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    value = proposal(json.loads(bounded_read(args.config, 2_000_000)),
                     json.loads(bounded_read(args.environments, 2_000_000)), project=args.project,
                     story=args.story, repository=args.repository, environment=args.environment)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    print("Unapproved proposal written; source, model and SUT calls: 0")


if __name__ == "__main__":
    main()
