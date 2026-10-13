"""Provider contracts. Only methods with a Milestone A consumer are defined.

Later milestones add implementations (Jira Cloud/DC REST, Bitbucket, Grok, verified
Playground env, object storage) behind the same contracts.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class IssueTrackerProvider(ABC):
    """READ-ONLY. Implementations: MockJiraProvider (fixtures), JiraCloudProvider, JiraDataCenterProvider.
    Write operations (add_comment / create_issue / transition) are intentionally absent."""
    @abstractmethod
    def get_issue(self, key: str) -> dict: ...

    @abstractmethod
    def acceptance_criteria(self, key: str) -> list[dict]: ...

    @abstractmethod
    def get_comments(self, key: str) -> list[dict]: ...

    @abstractmethod
    def list_attachments(self, key: str) -> list[dict]: ...

    @abstractmethod
    def get_attachment(self, key: str, attachment_id: str) -> dict: ...

    @abstractmethod
    def search_issues(self, project: str, text: str | None = None, max_results: int = 50) -> list[dict]:
        """Project-scoped search only; raw JQL is never accepted from callers."""


class GitProvider(ABC):
    """READ-ONLY repository access. Implementations: SyntheticGitProvider (fixtures), BitbucketCloudProvider,
    BitbucketDataCenterProvider. No clone, push, branch or PR operations."""
    @abstractmethod
    def list_repositories(self) -> list[dict]: ...

    @abstractmethod
    def get_repository(self, repo: str) -> dict: ...

    @abstractmethod
    def read_file(self, repo: str, path: str, ref: str = "main") -> str: ...

    @abstractmethod
    def list_dir(self, repo: str, path: str = "", ref: str = "main") -> list[dict]: ...

    @abstractmethod
    def supports_code_search(self) -> bool: ...

    @abstractmethod
    def search_code(self, repo: str, query: str, max_results: int = 25) -> list[dict]: ...

    @abstractmethod
    def repo_context(self, repo: str, ref: str | None = None) -> dict: ...


class ModelProvider(ABC):
    name: str
    label: str  # shown to the user, e.g. "REPLAYED (mock model)"

    @abstractmethod
    def complete(self, task: str, context: dict) -> dict:
        """Return a JSON object for a named task. Callers validate against a schema."""


class TestExecutor(ABC):
    @abstractmethod
    def run(self, run_id: str, plan: dict, env: dict, on_event) -> list[dict]: ...


class EnvironmentProvider(ABC):
    @abstractmethod
    def resolve(self, name: str) -> dict: ...

    @abstractmethod
    def is_url_allowed(self, env: dict, url: str) -> bool: ...

    @abstractmethod
    def list_environments(self) -> list[dict]:
        """Every registered environment with its verification status and whether it is executable."""


class SkillRegistry(ABC):
    @abstractmethod
    def get(self, name: str) -> Any: ...

    @abstractmethod
    def list(self) -> list[dict]: ...


class ArtifactStore(ABC):
    @abstractmethod
    def path_for(self, run_id: str, rel: str): ...

    @abstractmethod
    def write_bytes(self, run_id: str, rel: str, data: bytes) -> str: ...

    @abstractmethod
    def manifest(self, run_id: str) -> list[dict]: ...


class ApprovalProvider(ABC):
    @abstractmethod
    def record(self, run_id: str, plan_hash: str, approver: str, decision: str, comment: str = "") -> dict: ...

    @abstractmethod
    def is_approved(self, run_id: str, plan_hash: str) -> bool: ...
