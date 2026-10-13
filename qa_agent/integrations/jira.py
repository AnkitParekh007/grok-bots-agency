"""Read-only Jira adapters. JiraCloudProvider (REST v3, ADF, nextPageToken search) and JiraDataCenterProvider
(REST v2, startAt pagination). Only GET; project allowlist enforced on input keys AND returned issues."""
from __future__ import annotations
from urllib.parse import quote
from ..interfaces import IssueTrackerProvider
from .http import ReadOnlyHttpClient, IntegrationError
from .common import project_of, check_project, adf_text, extract_acceptance_criteria, attachment_meta, ProjectNotAllowed

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

    def _normalize(self, raw: dict) -> dict:
        f = raw.get("fields") or {}
        proj = (f.get("project") or {}).get("key") or project_of(raw.get("key", ""))
        if proj not in self.projects:
            raise ProjectNotAllowed(f"returned issue belongs to non-allowlisted project {proj}")
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
        return self._normalize(raw)

    def acceptance_criteria(self, key):
        return extract_acceptance_criteria(self.get_issue(key)["fields"], self.ac_field)

    def get_comments(self, key):
        key, out, start = self._key(key), [], 0
        for _ in range(self.max_pages):
            d = self.http.get_json(f"rest/api/{self.api}/issue/{key}/comment", {"startAt": start, "maxResults": self.page_size})
            cs = d.get("comments") or []
            out += [{"id": str(c.get("id")), "author": (c.get("author") or {}).get("displayName"),
                     "created": c.get("created"), "body": adf_text(c.get("body")).strip()} for c in cs]
            start += len(cs)
            if not cs or start >= int(d.get("total", 0)):
                return out
        return out  # truncated at max_pages (bounded)

    def list_attachments(self, key):
        return self.get_issue(key)["fields"]["attachment"]

    def get_attachment(self, key, attachment_id):
        meta = {a["id"]: a for a in self.list_attachments(key)}.get(str(attachment_id))
        if not meta:
            raise IntegrationError(f"attachment {attachment_id} not on {key}")
        if (meta.get("size") or 0) > self.max_attachment_bytes:
            raise IntegrationError(f"attachment {attachment_id} exceeds max_attachment_bytes")
        r = self.http.request("GET", self._content_path(meta), accept="*/*")
        data = r.content[: self.max_attachment_bytes]
        text = data.decode("utf-8", "replace") if (meta.get("mime_type") or "").startswith(TEXT_MIME) else None
        return {**meta, "bytes": len(data), "text": text}

    def search_issues(self, project, text=None, max_results=50):
        project = check_project(project, self.projects)
        jql = f"project = {project}" + (f" AND text ~ {_quote_jql(text)}" if text else "") + " ORDER BY key ASC"
        out = []
        for page in self._search_pages(jql, max_results):
            out += [self._normalize(i) for i in page]
            if len(out) >= max_results:
                break
        return [{"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"],
                 "labels": i["fields"]["labels"]} for i in out[:max_results]]


class JiraCloudProvider(_JiraBase):
    kind, api = "jira_cloud", "3"

    def _search_pages(self, jql, max_results):
        token = None
        for _ in range(self.max_pages):
            p = {"jql": jql, "maxResults": min(self.page_size, max_results), "fields": "summary,status,labels,project"}
            if token:
                p["nextPageToken"] = token
            d = self.http.get_json("rest/api/3/search/jql", p)
            yield d.get("issues") or []
            token = d.get("nextPageToken")
            if not token or d.get("isLast"):
                return

    def _content_path(self, meta):
        return f"rest/api/3/attachment/content/{meta['id']}"


class JiraDataCenterProvider(_JiraBase):
    kind, api = "jira_dc", "2"

    def _search_pages(self, jql, max_results):
        start = 0
        for _ in range(self.max_pages):
            d = self.http.get_json("rest/api/2/search", {"jql": jql, "startAt": start, "maxResults": min(self.page_size, max_results),
                                                          "fields": "summary,status,labels,project"})
            issues = d.get("issues") or []
            yield issues
            start += len(issues)
            if not issues or start >= int(d.get("total", 0)):
                return

    def _content_path(self, meta):
        return f"secure/attachment/{quote(meta['id'])}/{quote(meta['filename'] or '')}"
