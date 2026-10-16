"""Incomplete or redirected collection snapshots must not be accepted as complete."""
import time
import json
from unittest.mock import patch

import httpx
import pytest

from qa_agent.integrations.http import IntegrationError, IntegrationNotAuthorized
from qa_agent.integrations.jira import JiraCloudProvider, JiraDataCenterProvider
from qa_agent.integrations.bitbucket import BitbucketCloudProvider, BitbucketDataCenterProvider
from qa_agent.integrations.pagination import IncompleteSource
from qa_agent.util import sha256
from tests.test_c0_integrations import cfg

pytestmark = [pytest.mark.security, pytest.mark.provider]


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setenv("C0_TEST_TOKEN", "synthetic-contract-token")


def adapter(provider, reply, **options):
    calls = []
    def handler(request):
        calls.append(request)
        value = reply(request, len(calls)) if callable(reply) else reply
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
    return provider(cfg("http://127.0.0.1", **options), transport=httpx.MockTransport(handler)), calls


@pytest.mark.parametrize("provider", [JiraCloudProvider, JiraDataCenterProvider])
def test_comment_page_cap_refuses_silent_partial_source(provider):
    a, calls = adapter(provider, {"comments": [{"id": "one", "body": "synthetic"}], "total": 2, "startAt": 0}, max_pages=1)
    with pytest.raises(IncompleteSource, match="page budget"):
        a.get_comments("PCC-1")
    assert len(calls) == 1


@pytest.mark.parametrize("value", [{"comments": [], "total": 2}, {"comments": []}, {"comments": {}, "total": 0},
                                   {"comments": [{"id": "one"}], "total": 1, "startAt": 10}])
def test_inconsistent_comment_pages_are_not_empty_success(value):
    a, _ = adapter(JiraCloudProvider, value)
    with pytest.raises(IncompleteSource): a.get_comments("PCC-1")


def test_repeated_comment_identity_stops_instead_of_inventing_progress():
    a, calls = adapter(JiraCloudProvider, lambda req, n: {"comments": [{"id": "repeated"}], "total": 3, "startAt": n - 1})
    with pytest.raises(IncompleteSource, match="identities"): a.get_comments("PCC-1")
    assert len(calls) == 2


@pytest.mark.parametrize("provider", [JiraCloudProvider, JiraDataCenterProvider])
def test_search_page_cap_is_distinct_from_requested_prefix_limit(provider):
    issue = {"key": "PCC-1", "fields": {"project": {"key": "PCC"}}}
    response = {"issues": [issue], "nextPageToken": "next"} if provider is JiraCloudProvider else {"issues": [issue], "total": 3}
    a, _ = adapter(provider, response, max_pages=1)
    assert len(a.search_issues("PCC", max_results=1)) == 1  # Explicit bounded search prefix.
    with pytest.raises(IncompleteSource, match="page budget"): a.search_issues("PCC", max_results=3)


@pytest.mark.parametrize("response", [{"issues": [], "isLast": False}, {"issues": [], "isLast": "false"}, {"issues": []}])
def test_jira_search_requires_consistent_terminal_marker(response):
    a, _ = adapter(JiraCloudProvider, response)
    with pytest.raises(IncompleteSource): a.search_issues("PCC")


@pytest.mark.parametrize("next_url", ["http://127.0.0.1/2.0/repositories/OTHER?page=2", "http://other.invalid/2.0/repositories/SYN?page=2",
                                     "http://127.0.0.1/2.0/repositories/SYN?page=2&q=unapproved", "http://127.0.0.1/2.0/repositories/SYN?page=2&pagelen=999"])
def test_cloud_continuations_cannot_change_resource_scope_or_page_budget(next_url):
    a, calls = adapter(BitbucketCloudProvider, {"values": [], "next": next_url})
    with pytest.raises((IncompleteSource, IntegrationNotAuthorized)): a.list_repositories()
    assert len(calls) == 1


@pytest.mark.parametrize("provider", [BitbucketCloudProvider, BitbucketDataCenterProvider])
def test_repository_page_cap_fails_explicitly(provider):
    response = {"values": [], "next": "http://127.0.0.1/2.0/repositories/SYN?page=2"} if provider is BitbucketCloudProvider else {"values": [], "isLastPage": False, "nextPageStart": 1}
    a, calls = adapter(provider, response, max_pages=1)
    with pytest.raises(IncompleteSource, match="page budget"): a.list_repositories()
    assert len(calls) == 1


@pytest.mark.parametrize("response", [{"values": [], "isLastPage": False, "nextPageStart": 0}, {"values": []}, {"values": [], "isLastPage": "true"}])
def test_data_center_requires_advancing_or_terminal_continuation(response):
    a, _ = adapter(BitbucketDataCenterProvider, response)
    with pytest.raises(IncompleteSource): a.list_repositories()


def test_collection_item_budget_and_request_deadline_are_enforced():
    a, _ = adapter(JiraCloudProvider, {"comments": [{"id": "one"}, {"id": "two"}], "total": 2}, max_collection_items=1)
    with pytest.raises(IncompleteSource, match="item budget"): a.get_comments("PCC-1")
    with pytest.raises(IntegrationError, match="deadline"):
        a.http.get_json("rest/api/3/issue/PCC-1", deadline=time.monotonic() - 1)


def test_retry_backoff_cannot_exceed_remaining_collection_deadline():
    a, calls = adapter(JiraCloudProvider, httpx.Response(503), backoff_base_s=1, max_backoff_s=2)
    sleeps = []
    a.http.sleep = sleeps.append
    with pytest.raises(IntegrationError, match="retry deadline"):
        a.http.get_json("rest/api/3/issue/PCC-1", deadline=time.monotonic() + 0.05)
    assert len(calls) == 1 and not sleeps


def test_attachment_content_is_not_silently_sliced_or_mislabeled_complete():
    a, _ = adapter(JiraCloudProvider, httpx.Response(200, content=b"12345"), max_attachment_bytes=4)
    with patch.object(a, "list_attachments", return_value=[{"id": "one", "size": 3, "mime_type": "text/plain"}]):
        with pytest.raises(IncompleteSource, match="size"): a.get_attachment("PCC-1", "one")


def test_unsupported_attachment_encoding_is_not_replaced_by_invented_text():
    a, _ = adapter(JiraCloudProvider, httpx.Response(200, content=b"\xff\xfe"))
    with patch.object(a, "list_attachments", return_value=[{"id": "one", "size": 2, "mime_type": "text/plain"}]):
        with pytest.raises(IncompleteSource, match="encoding"): a.get_attachment("PCC-1", "one")


@pytest.mark.parametrize("provider", [BitbucketCloudProvider, BitbucketDataCenterProvider])
@pytest.mark.parametrize("body", [b"\xff\xfe", b"binary\x00payload"])
def test_repository_text_never_masks_binary_or_unsupported_encoding(provider, body):
    a, _ = adapter(provider, httpx.Response(200, content=body))
    with pytest.raises(IncompleteSource, match="UTF-8"):
        a.read_file("SYN/pim-import-service", "README.md")


def test_readme_excerpt_retains_full_content_hash_and_explicit_completeness():
    body = "synthetic repository documentation\n" * 200
    def reply(req, n):
        if req.url.path.endswith("/src/main/") or req.url.path.endswith("/src/main"):
            return {"values": [{"path": "README.md", "type": "commit_file"}]}
        if req.url.path.endswith("README.md"):
            return httpx.Response(200, text=body)
        return {"full_name": "SYN/pim-import-service", "mainbranch": {"name": "main"}}
    a, _ = adapter(BitbucketCloudProvider, reply)
    context = a.repo_context("SYN/pim-import-service")
    assert len(context["readme"]) == 4000 and context["readme_complete"] is False
    assert context["readme_sha256"] == sha256(body) and context["top_level_pagination_complete"]
    assert context["resolved_commit"] is None


@pytest.mark.parametrize("status", [200, 503])
def test_original_worker_authority_is_rechecked_inside_source_requests_and_retries(tmp_path, status):
    from qa_agent.runtime import QAAgent
    from qa_agent.jobs import DurableJobs
    from qa_agent.workers import JobAccessDenied
    from tests.b1_helpers import agent_cfg
    a = QAAgent(tmp_path, agent_cfg=agent_cfg("replay"))
    jobs = DurableJobs(a.store)
    a.store.create_run("source-worker", "Synthetic source fence", "synthetic-local", "replay", "REPLAYED")
    jobs.enqueue("source-worker", "planning", {}, "a" * 64)
    job = jobs.claim("worker", "a" * 64)
    allowed, calls = [True], []
    def validate(current):
        if not allowed[0]: raise JobAccessDenied("synthetic caller revocation")
    def handler(request):
        calls.append(request);allowed[0] = False
        return httpx.Response(status, json={"key": "PCC-1", "fields": {"project": {"key": "PCC"}}})
    provider = JiraCloudProvider(cfg("http://127.0.0.1", backoff_base_s=0), gateway=a.gateway, transport=httpx.MockTransport(handler))
    try:
        with a.store.fenced_job(jobs, job, validate):
            with pytest.raises(JobAccessDenied): provider.get_issue("PCC-1")
        assert len(calls) == 1
        records = a.store.audit_rows("source-worker")
        intent = [r for r in records if r["tool"] == "integration.http.intent"]
        outcome = [r for r in records if r["tool"] == "integration.http"]
        assert len(intent) == len(outcome) == 1
        assert json.loads(intent[0]["args_summary"])["request_id"] == json.loads(outcome[0]["args_summary"])["request_id"]
    finally:
        provider.http._client.close();a.store.db.close()
