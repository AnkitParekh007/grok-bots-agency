"""Milestone C0: read-only Jira / Bitbucket adapters against LOCAL mock servers, environment safety, workspace selection.
No external hosts are contacted; all data is synthetic."""
import copy, json, os
import httpx, pytest
from qa_agent import ROOT
from qa_agent.devtools.integration_mocks import MockServer, EXPECTED_TOKEN
from qa_agent.environments import RegistryEnvironmentProvider, EnvironmentRefused
from qa_agent.integrations.http import (ReadOnlyHttpClient, IntegrationNotAuthorized, ReadOnlyViolation, IntegrationError,
                                        Unsupported, check_authorization)
from qa_agent.integrations.jira import JiraCloudProvider, JiraDataCenterProvider
from qa_agent.integrations.bitbucket import BitbucketCloudProvider, BitbucketDataCenterProvider, RepoNotAllowed
from qa_agent.integrations.common import ProjectNotAllowed
from qa_agent.integrations.registry import build_tracker, build_git
from qa_agent.interfaces import IssueTrackerProvider, GitProvider
from qa_agent.providers.mock_jira import MockJiraProvider
from qa_agent.providers.git_stub import SyntheticGitProvider
from qa_agent.runtime import QAAgent
from qa_agent.util import load_json, redact

pytestmark = [pytest.mark.security]
TOKEN_ENV = "C0_TEST_TOKEN"
USER_ENV = "C0_TEST_USER"


@pytest.fixture
def mock():
    with MockServer() as m:
        yield m


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, EXPECTED_TOKEN)
    monkeypatch.setenv(USER_ENV, "synthetic.user@example.invalid")


def cfg(url, **kw):
    c = {"enabled": True, "approved": True, "approval_ref": "TEST-ONLY", "base_url": url, "allowed_hosts": ["127.0.0.1"],
         "local_mock": True, "auth": {"type": "bearer", "token_env": TOKEN_ENV}, "timeout_s": 2, "max_retries": 2,
         "backoff_base_s": 0, "projects": ["PCC"], "repositories": ["SYN/pim-import-service"], "page_size": 2}
    c.update(kw)
    return c


@pytest.fixture
def agent(tmp_path):
    return QAAgent(tmp_path)


NOSLEEP = lambda s: None


# ---------------- contracts ----------------
@pytest.mark.parametrize("cls", [MockJiraProvider, JiraCloudProvider, JiraDataCenterProvider])
def test_tracker_contract_and_no_write_methods(cls):
    assert issubclass(cls, IssueTrackerProvider)
    for m in ("add_comment", "create_issue", "update_issue", "transition", "delete_issue"):
        assert not hasattr(cls, m)


@pytest.mark.parametrize("cls", [SyntheticGitProvider, BitbucketCloudProvider, BitbucketDataCenterProvider])
def test_git_contract_and_no_write_methods(cls):
    assert issubclass(cls, GitProvider)
    for m in ("clone", "push", "create_branch", "commit", "create_pull_request", "write_file"):
        assert not hasattr(cls, m)


def test_defaults_are_synthetic_and_real_adapters_disabled(agent):
    assert agent.tracker.kind == "mock_jira" and agent.git.kind == "synthetic_git"
    ic = load_json(ROOT / "config/agent.json")["integrations"]
    for name in ("jira_cloud", "jira_dc", "bitbucket_cloud", "bitbucket_dc"):
        assert ic[name]["enabled"] is False and ic[name]["approved"] is False and not ic[name]["base_url"]
        with pytest.raises(IntegrationNotAuthorized, match="disabled"):
            check_authorization(name, ic[name])


# ---------------- authorization (fail closed before any socket) ----------------
@pytest.mark.parametrize("mut,match", [
    ({"enabled": False}, "disabled"), ({"approved": False}, "not approved"), ({"approval_ref": None}, "not approved"),
    ({"base_url": None}, "base_url"), ({"allowed_hosts": []}, "allowed_hosts"),
    ({"local_mock": False}, "https required"), ({"auth": {"type": "bearer", "token_env": "C0_UNSET_ENV"}}, "not set"),
    ({"projects": [], "repositories": []}, "allowlist")])
def test_real_provider_cannot_connect_without_authorization(mock, mut, match):
    c = cfg(mock.url); c.update(mut)
    with pytest.raises(IntegrationNotAuthorized, match=match):
        JiraCloudProvider(c)
    assert mock.state.requests == []


def test_non_loopback_requires_https_even_with_local_mock_flag():
    c = cfg("http://jira.example.invalid", allowed_hosts=["jira.example.invalid"])
    with pytest.raises(IntegrationNotAuthorized):
        JiraDataCenterProvider(c)
    c["local_mock"] = False
    with pytest.raises(IntegrationNotAuthorized, match="https"):
        JiraDataCenterProvider(c)


def test_registry_refuses_real_tracker_when_disabled():
    c = load_json(ROOT / "config/agent.json")
    c["issue_tracker"]["provider"] = "jira_cloud"
    with pytest.raises(IntegrationNotAuthorized):
        build_tracker(c)
    c["git"]["provider"] = "bitbucket_dc"
    with pytest.raises(IntegrationNotAuthorized):
        build_git(c)
    c["git"]["provider"] = "github_anything"
    with pytest.raises(IntegrationNotAuthorized):
        build_git(c)


# ---------------- read-only enforcement ----------------
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_mutation_methods_rejected_before_network(mock, agent, method):
    cl = ReadOnlyHttpClient("jira_cloud", cfg(mock.url), gateway=agent.gateway)
    with pytest.raises(ReadOnlyViolation):
        cl.request(method, "rest/api/3/issue/PCC-DEMO-1")
    with pytest.raises(ReadOnlyViolation):
        cl.post("x")
    assert mock.state.requests == []
    assert agent.store.q("SELECT * FROM audit ORDER BY id")[-1]["decision"] == "deny"


def test_policy_must_allow_integration_http(mock, agent):
    del agent.gateway.policy["tools"]["integration.http"]
    j = JiraCloudProvider(cfg(mock.url), gateway=agent.gateway)
    with pytest.raises(ReadOnlyViolation, match="policy"):
        j.get_issue("PCC-DEMO-1")
    assert mock.state.requests == []


def test_absolute_url_to_other_origin_refused(mock):
    cl = ReadOnlyHttpClient("bitbucket_cloud", cfg(mock.url))
    with pytest.raises(IntegrationNotAuthorized):
        cl.get_json("http://127.0.0.1:1/2.0/repositories/SYN")
    with pytest.raises(IntegrationNotAuthorized):
        cl.get_json("https://evil.example.invalid/2.0/repositories/SYN")
    assert mock.state.requests == []


def test_all_mock_traffic_is_get_only(mock):
    j = JiraCloudProvider(cfg(mock.url)); j.get_issue("PCC-DEMO-1"); j.get_comments("PCC-DEMO-1"); j.search_issues("PCC")
    b = BitbucketDataCenterProvider(cfg(mock.url)); b.repo_context("SYN/pim-import-service")
    assert mock.state.requests and {r["method"] for r in mock.state.requests} == {"GET"}


# ---------------- Jira Cloud / DC behaviour ----------------
@pytest.mark.integration
@pytest.mark.parametrize("cls", [JiraCloudProvider, JiraDataCenterProvider])
def test_jira_issue_ac_comments_attachments(mock, cls):
    j = cls(cfg(mock.url))
    i = j.get_issue("PCC-DEMO-1")
    fx = load_json(ROOT / "fixtures/jira/PCC-DEMO-1.json")
    assert i["fields"]["summary"] == fx["fields"]["summary"] and "synthetic" in i["fields"]["labels"]
    assert "Acceptance criteria" in i["fields"]["description"]    # ADF (Cloud) flattened to text
    ac = j.acceptance_criteria("PCC-DEMO-1")
    assert len(ac) == 5 and ac[3]["text"].startswith("Rows without a supplier SKU")
    cs = j.get_comments("PCC-DEMO-1")                              # 2 comments, mock page cap 1-2 -> pagination
    assert [c["id"] for c in cs] == ["10001", "10002"] and "inch" in cs[0]["body"]
    at = j.list_attachments("PCC-DEMO-1")
    assert at[0]["filename"] == "supplier_valid.csv"
    body = j.get_attachment("PCC-DEMO-1", at[0]["id"])
    assert body["text"] == (ROOT / "fixtures/data/supplier_valid.csv").read_bytes().decode("utf-8")


@pytest.mark.integration
@pytest.mark.parametrize("cls", [JiraCloudProvider, JiraDataCenterProvider])
def test_jira_search_is_project_scoped_and_paginated(mock, cls):
    j = cls(cfg(mock.url, page_size=2))
    res = j.search_issues("PCC")
    assert [r["key"] for r in res] == sorted(load_json(p)["key"] for p in (ROOT / "fixtures/jira").glob("PCC-*.json"))
    searches = [r for r in mock.state.requests if "search" in r["path"]]
    assert len(searches) >= 4                                       # 7 issues / 2 per page
    assert all(r["query"]["jql"][0].startswith("project = PCC") for r in searches)
    assert [r["key"] for r in j.search_issues("PCC", "colour")] == ["PCC-DEMO-1"] or j.search_issues("PCC", "colour")


@pytest.mark.parametrize("cls", [JiraCloudProvider, JiraDataCenterProvider])
def test_jira_project_allowlist(mock, cls):
    j = cls(cfg(mock.url))
    for bad in (lambda: j.get_issue("SECRET-1"), lambda: j.search_issues("SECRET"), lambda: j.get_comments("SECRET-1")):
        with pytest.raises(ProjectNotAllowed):
            bad()
    with pytest.raises(ValueError):
        j.search_issues("PCC OR project = SECRET")
    with pytest.raises(ValueError):
        j.get_issue("../../admin")
    assert mock.state.requests == []


def test_jira_returned_issue_from_other_project_refused(mock):
    mock.state.issues["PCC-DEMO-1"] = copy.deepcopy(mock.state.issues["SECRET-1"]) | {"key": "PCC-DEMO-1"}
    with pytest.raises(ProjectNotAllowed):
        JiraDataCenterProvider(cfg(mock.url)).get_issue("PCC-DEMO-1")


def test_jql_text_is_quoted(mock):
    j = JiraCloudProvider(cfg(mock.url))
    j.search_issues("PCC", 'x" OR project = SECRET OR text ~ "y')
    jql = [r for r in mock.state.requests if "search" in r["path"]][0]["query"]["jql"][0]
    assert jql.startswith('project = PCC AND text ~ "x\\" OR project = SECRET')


# ---------------- retries / timeouts ----------------
@pytest.mark.parametrize("code", [429, 503])
def test_retry_on_429_5xx_bounded(mock, code):
    mock.state.faults = [code, code]
    j = JiraCloudProvider(cfg(mock.url, max_retries=2), sleep=NOSLEEP)
    assert j.get_issue("PCC-DEMO-1")["key"] == "PCC-DEMO-1" and len(mock.state.requests) == 3
    mock.state.requests.clear(); mock.state.faults = [code] * 5
    with pytest.raises(IntegrationError, match=str(code)):
        j.get_issue("PCC-DEMO-1")
    assert len(mock.state.requests) == 3


@pytest.mark.parametrize("code", [401, 403, 400, 404])
def test_no_retry_on_4xx(mock, code):
    mock.state.faults = [code]
    with pytest.raises(IntegrationNotAuthorized if code in (401, 403) else IntegrationError):
        JiraCloudProvider(cfg(mock.url), sleep=NOSLEEP).get_issue("PCC-DEMO-1")
    assert len(mock.state.requests) == 1


def test_wrong_credential_is_401_not_retried(mock, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "wrong-synthetic-token-value")
    with pytest.raises(IntegrationNotAuthorized, match="401"):
        BitbucketCloudProvider(cfg(mock.url), sleep=NOSLEEP).get_repository("SYN/pim-import-service")
    assert len(mock.state.requests) == 1


def test_timeout_is_enforced_and_bounded(mock):
    mock.state.delay_s = 0.6
    j = JiraCloudProvider(cfg(mock.url, timeout_s=0.2, max_retries=1), sleep=NOSLEEP)
    with pytest.raises(IntegrationError, match="Timeout"):
        j.get_issue("PCC-DEMO-1")
    assert len(mock.state.requests) == 2


# ---------------- redaction + audit ----------------
def test_credentials_never_in_errors_or_audit(mock, agent, monkeypatch):
    secret = "ATATT3xFfGF0-synthetic-secret-value-123456"
    monkeypatch.setenv(TOKEN_ENV, secret)
    c = cfg(mock.url, auth={"type": "basic", "user_env": USER_ENV, "token_env": TOKEN_ENV})
    j = JiraCloudProvider(c, gateway=agent.gateway, sleep=NOSLEEP)
    with pytest.raises(IntegrationNotAuthorized) as e:     # mock rejects this token -> 401
        j.get_issue("PCC-DEMO-1")
    blob = str(e.value) + json.dumps(agent.store.q("SELECT * FROM audit ORDER BY id")) + (agent.gateway.audit_jsonl.read_text())
    assert secret not in blob and "synthetic.user@example.invalid" not in json.dumps(agent.store.q("SELECT * FROM audit ORDER BY id"))
    import base64
    basic = base64.b64encode(f"synthetic.user@example.invalid:{secret}".encode()).decode()
    assert basic not in blob and secret not in redact(f"Authorization: Basic {basic} {secret}")
    assert basic not in redact(f"Authorization: Basic {basic}")


def test_every_http_call_audited_in_hash_chain(mock, agent):
    j = JiraDataCenterProvider(cfg(mock.url), gateway=agent.gateway)
    j.get_comments("PCC-DEMO-1")
    rows = [r for r in agent.store.q("SELECT * FROM audit ORDER BY id") if r["tool"] == "integration.http"]
    assert len(rows) == len(mock.state.requests) >= 1 and all(r["decision"] == "allow" for r in rows)
    assert EXPECTED_TOKEN not in json.dumps(rows)
    assert agent.store.verify_audit_chain()["ok"] is True


# ---------------- Bitbucket Cloud / DC ----------------
@pytest.mark.integration
@pytest.mark.parametrize("cls", [BitbucketCloudProvider, BitbucketDataCenterProvider])
def test_bitbucket_discovery_files_listing_context(mock, cls):
    b = cls(cfg(mock.url))
    repos = b.list_repositories()
    assert [r["full_name"] for r in repos] == ["SYN/pim-import-service"]     # non-allowlisted repo filtered out
    assert b.get_repository("SYN/pim-import-service")["default_branch"] == "main"
    top = b.list_dir("SYN/pim-import-service")
    assert {e["path"] for e in top} == {"README.md", "src", "tests"}             # page cap 2 -> paginated
    src = {e["path"] for e in b.list_dir("SYN/pim-import-service", "src")}
    assert src == {"src/importer.py", "src/units.py"}
    assert "SKU_REQUIRED" in b.read_file("SYN/pim-import-service", "src/importer.py")
    ctx = b.repo_context("SYN/pim-import-service")
    assert ctx["readme"].startswith("# pim-import-service (SYNTHETIC)")


def test_bitbucket_code_search_cloud_flag_and_dc_unsupported(mock):
    with pytest.raises(Unsupported):
        BitbucketCloudProvider(cfg(mock.url)).search_code("SYN/pim-import-service", "SKU_REQUIRED")
    hits = BitbucketCloudProvider(cfg(mock.url, code_search_enabled=True)).search_code("SYN/pim-import-service", "SKU_REQUIRED")
    assert {h["path"] for h in hits} == {"src/importer.py", "README.md"} or "src/importer.py" in {h["path"] for h in hits}
    dc = BitbucketDataCenterProvider(cfg(mock.url, code_search_enabled=True))
    assert dc.supports_code_search() is False
    with pytest.raises(Unsupported):
        dc.search_code("SYN/pim-import-service", "SKU")


@pytest.mark.parametrize("cls", [BitbucketCloudProvider, BitbucketDataCenterProvider, None])
def test_bitbucket_repo_allowlist_and_path_traversal(mock, cls):
    b = cls(cfg(mock.url)) if cls else SyntheticGitProvider(ROOT / "fixtures/repos", ["SYN/pim-import-service"])
    with pytest.raises(RepoNotAllowed):
        b.read_file("SYN/not-allowlisted-repo", "README.md")
    with pytest.raises(ValueError):
        b.read_file("SYN/pim-import-service", "../../../../etc/passwd")
    with pytest.raises(ValueError):
        b.read_file("SYN/pim-import-service", "README.md", ref="main;rm -rf")
    assert mock.state.requests == []


# ---------------- environments ----------------
def _verified_env(**kw):
    e = {"kind": "playground", "base_url": "https://playground.example.invalid", "status": "verified", "production": False,
         "data_class": "synthetic", "allowed_hosts": ["playground.example.invalid"],
         "safety_verification": {"verified_by": "qa.lead", "verified_at": "2026-10-13", "approval_ref": "SYN-1"},
         "test_accounts": [{"id": "qa-bot-1", "synthetic": True, "credential_env": TOKEN_ENV}],
         "cleanup": {"procedure_ref": "docs/cleanup.md", "steps": ["delete synthetic SKUs with prefix SYN-"]}}
    e.update(kw)
    return e


def test_dev_and_playground_unverified_by_default(agent):
    envs = {e["name"]: e for e in agent.envs.list_environments()}
    for n in ("bluemeteor-dev", "bluemeteor-playground"):
        assert envs[n]["status"] == "unverified" and envs[n]["executable"] is False
        with pytest.raises(EnvironmentRefused):
            agent.envs.resolve(n)
    assert envs["synthetic-local"]["executable"] is True


@pytest.mark.parametrize("mut,match", [
    ({"status": "unverified"}, "safety verification"), ({"safety_verification": {"verified_by": "x"}}, "safety_verification"),
    ({"allowed_hosts": []}, "allowed_hosts"), ({"base_url": "http://playground.example.invalid"}, "https"),
    ({"test_accounts": []}, "test accounts"), ({"test_accounts": [{"id": "real", "synthetic": False, "credential_env": TOKEN_ENV}]}, "test accounts"),
    ({"test_accounts": [{"id": "a", "synthetic": True, "credential_env": "C0_UNSET_ENV"}]}, "not set"),
    ({"cleanup": None}, "cleanup"), ({"cleanup": {"procedure_ref": "x", "steps": []}}, "cleanup"),
    ({"data_class": "customer"}, "synthetic")])
def test_non_prod_env_requires_every_safety_control(mut, match):
    reg = RegistryEnvironmentProvider({"environments": {"pg": _verified_env(**mut)}})
    with pytest.raises(EnvironmentRefused, match=match):
        reg.resolve("pg")


def test_fully_verified_env_resolves_and_urls_scoped():
    reg = RegistryEnvironmentProvider({"environments": {"pg": _verified_env()}})
    e = reg.resolve("pg")
    assert reg.is_url_allowed(e, "https://playground.example.invalid/api/x")
    assert not reg.is_url_allowed(e, "http://playground.example.invalid/api/x")
    assert not reg.is_url_allowed(e, "https://other.example.invalid/")


@pytest.mark.parametrize("name,mut", [("prod", {}), ("bluemeteor-production", {}), ("pg", {"production": True}),
                                      ("pg", {"production": None}), ("pg", {"kind": "production"}),
                                      ("pg", {"base_url": "https://app.bluemeteor.com", "allowed_hosts": ["app.bluemeteor.com"]}),
                                      ("pg", {"base_url": "https://pim.prod.example.invalid", "allowed_hosts": ["pim.prod.example.invalid"]})])
def test_production_hard_blocked(name, mut):
    pats = load_json(ROOT / "config/environments.json")["prohibited_host_patterns"]
    reg = RegistryEnvironmentProvider({"prohibited_host_patterns": pats, "environments": {name: _verified_env(**mut)}})
    with pytest.raises(EnvironmentRefused, match="prohibited"):
        reg.resolve(name)


# ---------------- workspace selection ----------------
def _client(tmp_path):
    from fastapi.testclient import TestClient
    from qa_agent.server import create_app
    return TestClient(create_app(tmp_path / "d", auth_mode="demo"))


@pytest.mark.approval
def test_local_auth_boundaries_on_selection_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from qa_agent.server import create_app
    from qa_agent.auth import UserStore
    us = UserStore(tmp_path / "users.json")
    viewer, req = us.add("vic", "viewer"), us.add("rita", "requester")
    c = TestClient(create_app(tmp_path / "d", auth_mode="local", users_file=tmp_path / "users.json"))
    for u in ("/api/projects", "/api/projects/PCC/stories", "/api/environments", "/api/projects/PCC/repositories"):
        assert c.get(u).status_code == 401
        assert c.get(u, headers={"Authorization": f"Bearer {viewer}"}).status_code == 200
    body = {"message": "test", "project": "PCC", "story_key": "PCC-DEMO-1"}
    assert c.post("/api/chat", json=body, headers={"Authorization": f"Bearer {viewer}"}).status_code == 403
    r = c.post("/api/chat", json=body, headers={"Authorization": f"Bearer {req}"})
    assert r.status_code == 200
    a = c.app.state.agent
    a.threads[-1].join(60) if a.threads else None
    run = a.store.get_run(r.json()["run_id"])
    assert run["requested_by"] == "rita" and run["project"] == "PCC"
    # requester cannot approve (role boundary preserved)
    assert c.post(f"/api/runs/{run['id']}/approval", json={"decision": "approved", "plan_hash": run["plan_hash"]},
                  headers={"Authorization": f"Bearer {req}"}).status_code == 403


@pytest.mark.approval
def test_workspace_selection_endpoints(tmp_path):
    c = _client(tmp_path)
    ps = c.get("/api/projects").json()
    assert ps[0]["key"] == "PCC" and ps[0]["tracker"] == "mock_jira" and ps[0]["git"] == "synthetic_git"
    keys = [s["key"] for s in c.get("/api/projects/PCC/stories").json()]
    assert "PCC-DEMO-1" in keys
    st = c.get("/api/projects/PCC/stories/PCC-DEMO-1").json()
    assert len(st["acceptance_criteria"]) == 5 and len(st["comments"]) == 2 and st["attachments"][0]["filename"] == "supplier_valid.csv"
    assert c.get("/api/projects/PCC/stories/OTHER-1").status_code == 400
    assert c.get("/api/projects/NOPE/stories").status_code == 404
    assert [r["full_name"] for r in c.get("/api/projects/PCC/repositories").json()] == ["SYN/pim-import-service"]
    assert c.get("/api/projects/PCC/repositories/SYN/pim-import-service/context").json()["readme"]
    assert c.get("/api/projects/PCC/repositories/SYN/other/context").status_code == 404
    envs = {e["name"]: e for e in c.get("/api/environments").json()}
    assert envs["bluemeteor-playground"]["executable"] is False
    assert c.get("/api/config").json()["environments"] == ["synthetic-local"]
    tools = {r["tool"] for r in c.app.state.agent.store.q("SELECT * FROM audit ORDER BY id")}
    assert {"tracker.search", "tracker.get_issue", "tracker.get_comments", "git.list_repos", "git.repo_context", "env.list"} <= tools


@pytest.mark.approval
def test_chat_selection_validated_and_recorded(tmp_path):
    c = _client(tmp_path)
    bad = [{"project": "NOPE"}, {"project": "PCC", "story_key": "OTHER-1"}, {"project": "PCC", "repository": "SYN/other"},
           {"repository": "SYN/pim-import-service"}, {"project": "PCC", "environment": "unlisted-env"}]
    for b in bad:
        expected = 404 if b.get("project") == "NOPE" else 400
        assert c.post("/api/chat", json={"message": "test PCC-DEMO-1", **b}).status_code == expected
    a = c.app.state.agent
    rid = a.start_run("test it", "synthetic-local", story_key="PCC-DEMO-1", project="PCC", repository="SYN/pim-import-service", wait=True)
    r = a.store.get_run(rid)
    assert r["status"] == "awaiting_approval" and r["project"] == "PCC" and r["repo_context_json"]["ref"] == "main"
    assert any(e["step"] == "repository" for e in a.store.events(rid))
    assert any(x["tool"] == "git.repo_context" and x["decision"] == "allow" for x in a.store.audit_rows(rid))
    assert not a.approvals.is_approved(rid, r["plan_hash"])          # selection never implies approval


@pytest.mark.approval
def test_unverified_env_run_fails_closed(agent):
    with pytest.raises(ValueError):
        agent.start_run("x", "unlisted", story_key="PCC-DEMO-1", project="PCC")
    rid = agent.start_run("test", "bluemeteor-playground", story_key="PCC-DEMO-1", project="PCC", wait=True)
    r = agent.store.get_run(rid)
    assert r["status"] == "failed" and "not executable" in r["error"] and r["plan_hash"] is None


def test_real_tracker_stories_not_auto_classified_synthetic(mock, tmp_path, monkeypatch):
    c = load_json(ROOT / "config/agent.json")
    c["issue_tracker"]["provider"] = "jira_dc"
    c["integrations"]["jira_dc"] = cfg(mock.url)
    a = QAAgent(tmp_path, agent_cfg=c)
    assert a.tracker.kind == "jira_dc"
    rid = a.start_run("test", "synthetic-local", story_key="PCC-DEMO-1", project="PCC", wait=True)
    ev = " ".join(e["message"] for e in a.store.events(rid))
    assert "Data classification for model input: unclassified" in ev
    assert {r["method"] for r in mock.state.requests} == {"GET"}
