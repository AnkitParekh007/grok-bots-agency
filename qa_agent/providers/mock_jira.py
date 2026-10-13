import re
from pathlib import Path
from ..interfaces import IssueTrackerProvider
from ..util import load_json
from ..integrations.common import extract_acceptance_criteria, attachment_meta, check_project, project_of
from .. import ROOT


class MockJiraProvider(IssueTrackerProvider):
    """Serves Jira-REST-shaped SYNTHETIC fixtures from disk. Read-only; no network."""
    kind = "mock_jira"

    def __init__(self, fixtures_dir: Path, extras_dir: Path | None = None, projects=None):
        self.dir = Path(fixtures_dir)
        self.extras = Path(extras_dir) if extras_dir else self.dir.parent / "jira_extras"
        self.projects = list(projects) if projects else None

    def get_issue(self, key):
        if not re.fullmatch(r"[A-Z][A-Z0-9]+-[A-Z0-9-]+", key or ""):
            raise ValueError(f"invalid issue key {key!r}")
        if self.projects is not None:
            check_project(project_of(key), self.projects)
        p = self.dir / f"{key}.json"
        if not p.exists():
            raise KeyError(f"issue {key} not found in mock tracker")
        return load_json(p)

    def _extra(self, key):
        self.get_issue(key)
        p = self.extras / f"{key}.json"
        return load_json(p) if p.exists() else {}

    def acceptance_criteria(self, key):
        return extract_acceptance_criteria(self.get_issue(key)["fields"])

    def get_comments(self, key):
        return [{"id": c["id"], "author": c["author"]["displayName"], "created": c["created"], "body": c["body"]}
                for c in self._extra(key).get("comments", [])]

    def list_attachments(self, key):
        return [attachment_meta({**a, "size": (ROOT / a["fixture"]).stat().st_size}) for a in self._extra(key).get("attachments", [])]

    def get_attachment(self, key, attachment_id):
        for a in self._extra(key).get("attachments", []):
            if a["id"] == str(attachment_id):
                data = (ROOT / a["fixture"]).read_bytes()
                return {**attachment_meta({**a, "size": len(data)}), "bytes": len(data), "text": data.decode("utf-8", "replace")}
        raise KeyError(f"attachment {attachment_id} not on {key}")

    def search_issues(self, project, text=None, max_results=50):
        if self.projects is not None:
            check_project(project, self.projects)
        out = []
        for p in sorted(self.dir.glob(f"{project}-*.json")):
            i = load_json(p)
            if text and text.lower() not in (i["fields"]["summary"] + " " + i["fields"].get("description", "")).lower():
                continue
            out.append({"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"],
                        "labels": i["fields"].get("labels", [])})
        return out[:max_results]
