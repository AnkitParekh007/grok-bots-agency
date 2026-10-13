"""Builds tracker / git providers from config. Default: fixture-backed synthetic providers (no network).
Real adapters are only constructed when selected AND check_authorization() passes (fail closed)."""
from .. import ROOT
from .http import IntegrationNotAuthorized
from .jira import JiraCloudProvider, JiraDataCenterProvider
from .bitbucket import BitbucketCloudProvider, BitbucketDataCenterProvider

TRACKERS = {"jira_cloud": JiraCloudProvider, "jira_dc": JiraDataCenterProvider}
GITS = {"bitbucket_cloud": BitbucketCloudProvider, "bitbucket_dc": BitbucketDataCenterProvider}


def build_tracker(cfg: dict, gateway=None, transport=None):
    from ..providers.mock_jira import MockJiraProvider
    tc = cfg.get("issue_tracker") or {}
    name = tc.get("provider", "mock_jira")
    if name == "mock_jira":
        return MockJiraProvider(ROOT / tc.get("fixtures_dir", "fixtures/jira"))
    if name not in TRACKERS:
        raise IntegrationNotAuthorized(f"unknown issue tracker provider {name!r}")
    return TRACKERS[name]((cfg.get("integrations") or {}).get(name) or {}, gateway=gateway, transport=transport)


def build_git(cfg: dict, gateway=None, transport=None):
    from ..providers.git_stub import SyntheticGitProvider
    gc = cfg.get("git") or {}
    name = gc.get("provider", "synthetic_git")
    if name == "synthetic_git":
        return SyntheticGitProvider(ROOT / gc.get("repos_dir", "fixtures/repos"))
    if name not in GITS:
        raise IntegrationNotAuthorized(f"unknown git provider {name!r}")
    return GITS[name]((cfg.get("integrations") or {}).get(name) or {}, gateway=gateway, transport=transport)
