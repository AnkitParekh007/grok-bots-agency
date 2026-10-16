"""A broad connector identity must not expand the resource requested by a caller."""
import httpx
import pytest
from qa_agent.integrations.jira import JiraCloudProvider, JiraDataCenterProvider
from qa_agent.integrations.common import ProjectNotAllowed
from qa_agent.integrations.bitbucket import BitbucketCloudProvider, BitbucketDataCenterProvider, RepoNotAllowed
from tests.test_c0_integrations import cfg

pytestmark = [pytest.mark.security]


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setenv("C0_TEST_TOKEN", "synthetic-test-token")


@pytest.mark.parametrize("provider", [JiraCloudProvider, JiraDataCenterProvider])
@pytest.mark.parametrize("operation", ["get", "search"])
def test_another_globally_allowlisted_project_is_still_rejected(provider, operation):
    issue = {"key": "OTHER-1", "fields": {"project": {"key": "OTHER"}}}
    response = issue if operation == "get" else {"issues": [issue], "isLast": True, "total": 1}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    adapter = provider(cfg("http://127.0.0.1", projects=["PCC", "OTHER"]), transport=transport)
    with pytest.raises(ProjectNotAllowed, match="requested"):
        adapter.get_issue("PCC-1") if operation == "get" else adapter.search_issues("PCC")


@pytest.mark.parametrize("provider", [BitbucketCloudProvider, BitbucketDataCenterProvider])
def test_repository_response_must_match_exact_requested_repository(provider):
    response = {"full_name": "SYN/other", "project": {"key": "SYN"}, "slug": "other"}
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=response)
    adapter = provider(cfg("http://127.0.0.1", repositories=["SYN/one", "SYN/other"]), transport=httpx.MockTransport(handler))
    with pytest.raises(RepoNotAllowed, match="requested"):
        adapter.get_repository("SYN/one")
    assert len(calls) == 1
