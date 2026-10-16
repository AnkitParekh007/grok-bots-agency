"""Typed MCP adapters. Results obey the same normalized contracts as REST reads.

Operator-reviewed tool bindings translate canonical parameters into server-specific
arguments; unsupported or incompatible payloads are refused, never guessed by a model.
"""
from .jira import JiraCloudProvider
from .bitbucket import BitbucketCloudProvider, check_repo, check_ref, clean_path, RepoNotAllowed
from .common import adf_text, check_project, project_of, attachment_meta
from .http import IntegrationError, Unsupported


def complete(payload, key):
    if not isinstance(payload, dict) or payload.get("complete") is not True or not isinstance(payload.get(key), list):
        raise IntegrationError("MCP collection completeness contract missing or incomplete")
    return payload[key]


class McpJiraProvider(JiraCloudProvider):
    kind = "jira_mcp"

    def __init__(self, cfg, client):
        self.cfg, self.client = cfg, client
        self.projects = cfg.get("projects", [])
        self.ac_field = cfg.get("acceptance_criteria_field")
        self.max_attachment_bytes = int(cfg.get("max_attachment_bytes", 1_000_000))

    def get_issue(self, key):
        self._key(key)
        value = self.client.read("issue.read", {"key": key})
        return self._normalize(value, expected_key=key, expected_project=project_of(key))

    def search_issues(self, project, text=None, max_results=50):
        check_project(project, self.projects)
        if text is not None and (not isinstance(text, str) or len(text) > 200):
            raise ValueError("invalid issue search text")
        limit = max(1, min(int(max_results), 100))
        raw = self.client.read("issue.search", {"project": project, "text": text or "", "limit": limit})
        rows = [self._normalize(i, expected_project=project) for i in complete(raw, "issues")]
        return [{"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"],
                 "labels": i["fields"]["labels"]} for i in rows[:limit]]

    def get_comments(self, key):
        self._key(key)
        raw = self.client.read("issue.comments", {"key": key})
        return [{"id": str(c["id"]), "author": (c.get("author") or {}).get("displayName"),
                 "created": c.get("created"), "body": adf_text(c.get("body")).strip()} for c in complete(raw, "comments")]

    def get_attachment(self, key, attachment_id):
        self._key(key)
        meta = next((a for a in self.list_attachments(key) if a["id"] == str(attachment_id)), None)
        if not meta or meta.get("size", 0) > self.max_attachment_bytes:
            raise IntegrationError("attachment is absent or exceeds the approved size")
        raw = self.client.read("issue.attachment", {"key": key, "attachment_id": str(attachment_id)})
        text = raw.get("text") if isinstance(raw, dict) else None
        if not isinstance(text, str) or len(text.encode()) > self.max_attachment_bytes:
            raise IntegrationError("MCP attachment content is unsupported or oversized")
        return {**meta, "text": text, "bytes": len(text.encode())}


class McpBitbucketProvider(BitbucketCloudProvider):
    kind = "bitbucket_mcp"

    def __init__(self, cfg, client):
        self.cfg, self.client, self.repos = cfg, client, cfg.get("repositories", [])
        self.max_file_bytes = int(cfg.get("max_file_bytes", 500_000))

    def list_repositories(self):
        return [self.get_repository(repo) for repo in self.repos]

    def get_repository(self, repo):
        check_repo(repo, self.repos)
        raw = self.client.read("repository.read", {"repository": repo})
        info = self._repo(raw)
        if info["full_name"] != repo:
            raise RepoNotAllowed("MCP repository does not match the requested resource")
        return info

    def read_file(self, repo, path, ref="main"):
        check_repo(repo, self.repos)
        path, ref = clean_path(path), check_ref(ref)
        raw = self.client.read("repository.file", {"repository": repo, "path": path, "ref": ref})
        if not isinstance(raw, dict) or any(raw.get(k) != v for k, v in {"repository": repo, "path": path, "ref": ref}.items()):
            raise IntegrationError("MCP file source identity mismatch")
        text = raw.get("text")
        if not isinstance(text, str) or len(text.encode()) > self.max_file_bytes:
            raise IntegrationError("MCP file content is unsupported or oversized")
        return text

    def list_dir(self, repo, path="", ref="main"):
        check_repo(repo, self.repos)
        path, ref = clean_path(path), check_ref(ref)
        raw = self.client.read("repository.directory", {"repository": repo, "path": path, "ref": ref})
        rows = complete(raw, "entries")
        out = []
        for row in rows:
            item = clean_path(row.get("path", ""))
            if not item or (path and not item.startswith(path + "/")) or row.get("type") not in ("file", "dir"):
                raise IntegrationError("MCP directory entry violates requested path scope")
            out.append({"path": item, "type": row["type"], "size": row.get("size")})
        return out

    def supports_code_search(self):
        return False

    def search_code(self, repo, query, max_results=25):
        raise Unsupported("MCP code search requires a separately reviewed result/source contract")
