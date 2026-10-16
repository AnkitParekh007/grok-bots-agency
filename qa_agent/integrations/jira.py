"""Read-only Jira adapters. JiraCloudProvider (REST v3, ADF, nextPageToken search) and JiraDataCenterProvider
(REST v2, startAt pagination). Only GET; project allowlist enforced on input keys AND returned issues."""
from __future__ import annotations
from urllib.parse import quote
from ..interfaces import IssueTrackerProvider
from .http import ReadOnlyHttpClient, IntegrationError
from .common import project_of, check_project, adf_text, extract_acceptance_criteria, attachment_meta, ProjectNotAllowed
from .pagination import CollectionBudget, IncompleteSource, offset_page
from ..util import sha256

ISSUE_FIELDS = "summary,description,labels,issuetype,status,project,attachment"
TEXT_MIME = ("text/", "application/json", "application/xml")


def _quote_jql(s: str) -> str:
    if len(s) > 200 or any(c in s for c in "\n\r"):
        raise ValueError("search text too long or multi-line")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _JiraBase(IssueTrackerProvider):
    kind = "jira"
    api = "2"

    def __init__(self, cfg: dict, gateway=None, run_id=None, transport=None, sleep=None):
        kw = {"sleep": sleep} if sleep else {}
        self.cfg = cfg
        self.http = ReadOnlyHttpClient(self.kind, cfg, gateway, run_id, transport, **kw)
        self.projects = list(cfg.get("projects") or [])
        self.page_size = max(1, min(int(cfg.get("page_size", 50)), 100))
        self.max_pages = max(1, int(cfg.get("max_pages", 10)))
        self.ac_field = cfg.get("acceptance_criteria_field")
        self.max_attachment_bytes = int(cfg.get("max_attachment_bytes", 1_000_000))

    def _key(self, key):
        check_project(project_of(key), self.projects)
        return key

    def _normalize(self, raw: dict, *, expected_key=None, expected_project=None) -> dict:
        f = raw.get("fields") or {}
        proj = (f.get("project") or {}).get("key") or project_of(raw.get("key", ""))
        if proj not in self.projects:
            raise ProjectNotAllowed("returned issue is outside the requested project scope")
        if project_of(raw.get("key", "")) != proj or (expected_key is not None and raw.get("key") != expected_key) or (expected_project is not None and proj != expected_project):
            raise ProjectNotAllowed("returned issue does not match the requested resource")
        return {"key": raw.get("key"), "_source": self.kind, "fields": {
            "summary": f.get("summary", ""), "description": adf_text(f.get("description")).strip(),
            "labels": f.get("labels") or [], "issuetype": {"name": (f.get("issuetype") or {}).get("name")},
            "status": {"name": (f.get("status") or {}).get("name")}, "project": {"key": proj},
            "attachment": [attachment_meta(a) for a in f.get("attachment") or []],
            **({self.ac_field: adf_text(f.get(self.ac_field))} if self.ac_field and f.get(self.ac_field) else {})}}

    # ---- IssueTrackerProvider ----
    def get_issue(self, key):
        raw = self.http.get_json(f"rest/api/{self.api}/issue/{self._key(key)}",
                                 {"fields": ISSUE_FIELDS + (f",{self.ac_field}" if self.ac_field else "")})
        return self._normalize(raw, expected_key=key, expected_project=project_of(key))

    def acceptance_criteria(self, key):
        return extract_acceptance_criteria(self.get_issue(key)["fields"], self.ac_field)

    def get_comments(self, key):
        key, out, start = self._key(key), [], 0
        budget = CollectionBudget(self.http, self.cfg)
        seen = set()
        for _ in range(budget.max_pages):
            d = budget.get(f"rest/api/{self.api}/issue/{key}/comment", {"startAt": start, "maxResults": self.page_size})
            cs = budget.rows(d, "comments")
            ids = [str(c.get("id")) for c in cs]
            if any(c.get("id") is None for c in cs) or len(set(ids)) != len(ids) or seen.intersection(ids):
                raise IncompleteSource("source comments have missing or repeated identities")
            seen.update(ids)
            out += [{"id": str(c.get("id")), "author": (c.get("author") or {}).get("displayName"),
                     "created": c.get("created"), "body": adf_text(c.get("body")).strip()} for c in cs]
            start, complete = offset_page(d, cs, start)
            if complete:
                return out
        raise IncompleteSource("source comments exceed page budget; retrieval is incomplete")

    def list_attachments(self, key):
        return self.get_issue(key)["fields"]["attachment"]

    def get_attachment(self, key, attachment_id):
        meta = {a["id"]: a for a in self.list_attachments(key)}.get(str(attachment_id))
        if not meta:
            raise IntegrationError(f"attachment {attachment_id} not on {key}")
        if type(meta.get("size")) is not int or meta["size"] < 0:
            raise IncompleteSource("attachment has no supported declared byte size")
        if (meta.get("size") or 0) > self.max_attachment_bytes:
            raise IntegrationError(f"attachment {attachment_id} exceeds max_attachment_bytes")
        r = self.http.request("GET", self._content_path(meta), accept="*/*")
        data = r.content
        if len(data) > self.max_attachment_bytes or (meta.get("size") is not None and len(data) != meta["size"]):
            raise IncompleteSource("attachment size differs or exceeds its byte budget; no truncated content returned")
        try:
            text = data.decode("utf-8") if (meta.get("mime_type") or "").startswith(TEXT_MIME) else None
        except UnicodeError:
            raise IncompleteSource("attachment text encoding is unsupported; source review required") from None
        return {**meta, "bytes": len(data), "text": text, "complete": True, "sha256": sha256(data)}

    def search_issues(self, project, text=None, max_results=50):
        project = check_project(project, self.projects)
        jql = f"project = {project}" + (f" AND text ~ {_quote_jql(text)}" if text else "") + " ORDER BY key ASC"
        out = []
        for page in self._search_pages(jql, max_results):
            out += [self._normalize(i, expected_project=project) for i in page]
            if len(out) >= max_results:
                break
        return [{"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"],
                 "labels": i["fields"]["labels"]} for i in out[:max_results]]


class JiraCloudProvider(_JiraBase):
    kind, api = "jira_cloud", "3"

    def _search_pages(self, jql, max_results):
        token, seen = None, set()
        budget = CollectionBudget(self.http, self.cfg)
        for _ in range(budget.max_pages):
            p = {"jql": jql, "maxResults": min(self.page_size, max_results), "fields": "summary,status,labels,project"}
            if token:
                p["nextPageToken"] = token
            d = budget.get("rest/api/3/search/jql", p)
            issues = budget.rows(d, "issues")
            token = d.get("nextPageToken")
            if ("isLast" in d and type(d["isLast"]) is not bool) or (d.get("isLast") is False and token is None) or (token is None and d.get("isLast") is not True):
                raise IncompleteSource("Jira search omits a consistent completion marker")
            if token is not None and (not isinstance(token, str) or not token or len(token) > 4096 or token in seen or d.get("isLast") is True):
                raise IncompleteSource("Jira search has an invalid or repeated continuation")
            if token is not None:
                seen.add(token)
            yield issues
            if not token or d.get("isLast"):
                return
        raise IncompleteSource("source search exceeds page budget; narrow the search")

    def _content_path(self, meta):
        return f"rest/api/3/attachment/content/{meta['id']}"


class JiraDataCenterProvider(_JiraBase):
    kind, api = "jira_dc", "2"

    def _search_pages(self, jql, max_results):
        start = 0
        budget = CollectionBudget(self.http, self.cfg)
        for _ in range(budget.max_pages):
            d = budget.get("rest/api/2/search", {"jql": jql, "startAt": start, "maxResults": min(self.page_size, max_results),
                                                          "fields": "summary,status,labels,project"})
            issues = budget.rows(d, "issues")
            next_start, complete = offset_page(d, issues, start)
            yield issues
            start = next_start
            if complete:
                return
        raise IncompleteSource("source search exceeds page budget; narrow the search")

    def _content_path(self, meta):
        return f"secure/attachment/{quote(meta['id'])}/{quote(meta['filename'] or '')}"
