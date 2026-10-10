"""Provider contracts. Only methods with a Milestone A consumer are defined.

Later milestones add implementations (Jira Cloud/DC REST, Bitbucket, Grok, verified
Playground env, object storage) behind the same contracts.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class IssueTrackerProvider(ABC):
    @abstractmethod
    def get_issue(self, key: str) -> dict: ...
    # Milestone C+: add_comment / create_issue (approval-gated, BL-140) - intentionally absent.


class GitProvider(ABC):
    """Interface stub only. No consumer in Milestone A; no implementation shipped.
    Milestone C: BitbucketReadProvider (Cloud or DC REST, read-only, scoped repos)."""
    @abstractmethod
    def read_file(self, repo: str, path: str, ref: str = "main") -> str: ...


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
