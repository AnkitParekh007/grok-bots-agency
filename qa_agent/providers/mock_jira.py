import re
from pathlib import Path
from ..interfaces import IssueTrackerProvider
from ..util import load_json


class MockJiraProvider(IssueTrackerProvider):
    """Serves Jira-REST-shaped fixtures from disk. Read-only; no network."""

    def __init__(self, fixtures_dir: Path):
        self.dir = Path(fixtures_dir)

    def get_issue(self, key):
        if not re.fullmatch(r"[A-Z][A-Z0-9]+-[A-Z0-9-]+", key or ""):
            raise ValueError(f"invalid issue key {key!r}")
        p = self.dir / f"{key}.json"
        if not p.exists():
            raise KeyError(f"issue {key} not found in mock tracker")
        return load_json(p)
