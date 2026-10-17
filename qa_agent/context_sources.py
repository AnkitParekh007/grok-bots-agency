"""Exact owner-selected contextual evidence. No discovery or model-selected reads."""
import re
import time
from datetime import datetime

from .providers.models import DataClassDenied
from .integrations.http import IntegrationError
from .util import canon, sha256, redact


def _require(ok, message):
    if not ok:
        raise DataClassDenied(message)


def _bytes(text):
    _require(isinstance(text, str), "context content must be text")
    try:
        return text.encode("utf-8")
    except UnicodeError:
        raise DataClassDenied("context content is not supported UTF-8") from None


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9TZ:+.-]{1,40}", value):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.isoformat() if stamp.tzinfo is not None else None
    except ValueError:
        return None


def selections(rule):
    items = rule.get("contexts", [])
    _require(isinstance(items, list) and len(items) <= 10, "context selections exceed supported bounds")
    seen = set()
    for item in items:
        _require(isinstance(item, dict), "invalid context selection")
        kind = item.get("kind")
        keys = {"kind", "sha256", "repository", "commit", "file"} if kind == "repository_file" else {"kind", "sha256", "id"}
        _require(kind in ("repository_file", "jira_comment", "jira_attachment") and set(item) == keys,
                 "unsupported contextual source contract")
        _require(isinstance(item["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]),
                 "reviewed context content digest required")
        if kind == "repository_file":
            _require(isinstance(item["repository"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80}", item["repository"]),
                     "invalid context repository")
            _require(isinstance(item["commit"], str) and re.fullmatch(r"[a-f0-9]{40}", item["commit"]),
                     "context repository source requires a full reviewed commit")
            path = item["file"]
            _require(isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9_. /-]{1,200}", path) and
                     all(part not in ("", ".", "..") for part in path.split("/")), "invalid context file path")
            identity = (kind, item["repository"], item["commit"], path)
        else:
            _require(isinstance(item["id"], str) and re.fullmatch(r"[1-9][0-9]{0,19}", item["id"]), "invalid Jira context id")
            identity = (kind, item["id"])
        _require(identity not in seen, "duplicate contextual source selection")
        seen.add(identity)
    return [dict(item) for item in items]


def collect(agent, run, rule, *, clock=time.monotonic):
    items = selections(rule)
    if not items:
        return []
    _require(canon(rule) == canon(agent.data_policy.source_rule(run)), "context selection differs from approved source policy")
    pc = agent.projects.get(run.get("project"), {})
    _require(pc.get("tenant_id", "local") == run.get("tenant_id", "local"), "context tenant differs from run")
    _require(isinstance(run.get("story_key"), str) and run["story_key"].startswith(pc.get("jira_project", "") + "-"),
             "context source issue differs from selected project")
    deadline, total, sources = clock() + 60, 0, []
    comments, attachments = None, None

    def call(tool, args, fn):
        _require(clock() < deadline, "context collection deadline exceeded")
        _require(canon(rule) == canon(agent.data_policy.source_rule(run)), "source approval changed during context collection")
        try:
            value = agent._tool(run["id"], tool, args, fn)
        except (IntegrationError, KeyError, UnicodeError):
            raise DataClassDenied("selected contextual evidence could not be read completely") from None
        _require(clock() < deadline, "context collection deadline exceeded")
        _require(canon(rule) == canon(agent.data_policy.source_rule(run)), "source approval changed during context collection")
        return value

    for item in items:
        locator, version = {}, None
        if item["kind"] == "repository_file":
            repo = item["repository"]
            _require(repo == run.get("repository") and repo in pc.get("repositories", []),
                     "context repository differs from selected run/project scope")
            git = agent.git_for(run["project"])
            text = call("git.read_file", {"repo": repo, "file": item["file"], "ref": item["commit"]},
                        lambda: git.read_file(repo, item["file"], item["commit"]))
            locator = {key: item[key] for key in ("repository", "commit", "file")}
            version = item["commit"]
        else:
            tracker = agent.tracker_for(run["project"])
            key = run["story_key"]
            locator = {"issue": key, "id": item["id"]}
            if item["kind"] == "jira_comment":
                if comments is None:
                    comments = call("tracker.get_comments", {"key": key}, lambda: tracker.get_comments(key))
                    _require(isinstance(comments, list) and len(comments) <= 200 and len(canon(comments).encode()) <= 200000,
                             "context comment inventory exceeds bounds")
                selected = [c for c in comments if isinstance(c, dict) and c.get("id") == item["id"]]
                _require(len(selected) == 1, "selected comment is missing or ambiguous")
                text = selected[0].get("body")
                stamp = selected[0].get("updated") or selected[0].get("created")
                version = _timestamp(stamp)
            else:
                if attachments is None:
                    attachments = call("tracker.list_attachments", {"key": key}, lambda: tracker.list_attachments(key))
                    _require(isinstance(attachments, list) and len(attachments) <= 200 and len(canon(attachments).encode()) <= 200000,
                             "context attachment inventory exceeds bounds")
                selected = [a for a in attachments if isinstance(a, dict) and a.get("id") == item["id"]]
                _require(len(selected) == 1, "selected attachment is missing or ambiguous")
                meta = selected[0]
                _require(type(meta.get("size")) is int and 0 < meta["size"] <= 100000 and isinstance(meta.get("mime_type"), str),
                         "selected attachment has unsupported size/type metadata")
                mime = meta["mime_type"].split(";", 1)[0]
                _require(mime.startswith("text/") or mime in ("application/json", "application/xml"),
                         "context attachment must be supported text")
                value = call("tracker.get_attachment", {"key": key, "attachment_id": item["id"]},
                             lambda: tracker.get_attachment(key, item["id"]))
                _require(isinstance(value, dict), "context attachment contract mismatch")
                text = value.get("text")
                _require(value.get("complete") is True and type(value.get("bytes")) is int and value["bytes"] == meta["size"] and
                         isinstance(text, str) and len(_bytes(text)) == value["bytes"] and value.get("sha256") == sha256(text),
                         "context attachment is incomplete or changed")
        _require(isinstance(text, str) and 0 < len(_bytes(text)) <= 100000 and "\x00" not in text,
                 "context content is empty, unsupported or exceeds byte limit")
        _require(sha256(text) == item["sha256"], "context bytes differ from owner-reviewed selection")
        content = redact(text).replace("\r\n", "\n").replace("\r", "\n")
        _require(len(_bytes(content)) <= 100000, "transformed context content exceeds byte limit")
        total += max(len(_bytes(text)), len(_bytes(content)))
        _require(total <= 200000, "combined contextual evidence exceeds byte limit")
        digest = sha256(content)
        source_id = "ctx:" + item["kind"] + ":" + sha256(canon(locator)) + ":" + digest
        sources.append({"id": source_id, "kind": item["kind"], "locator": locator, "content": content,
                        "sha256": digest, "observed_sha256": item["sha256"], "classification": rule["classification"],
                        "complete": True, "upstream_version": version,
                        "version_basis": "reviewed full commit and observed content" if item["kind"] == "repository_file" else "reviewed observed content",
                        "content_transform": "LF line endings and registered-secret redaction",
                        "line_start": 1, "line_end": len(content.split("\n"))})
    return sources


def verify(agent, run):
    """Re-read only the exact reviewed selection at approval/execution handoff."""
    bundle = run.get("source_bundle_json") or {}
    expected_ids = bundle.get("context_ids", [])
    raw = (agent.projects.get(run.get("project"), {}).get("source_policy") or {}).get("contexts", [])
    if not expected_ids and not raw:
        return
    rule = agent.data_policy.source_rule(run)
    _require(bundle.get("context_selection_sha256") == sha256(canon(selections(rule))), "reviewed context selection changed")
    sources = collect(agent, run, rule)
    original = [s for s in bundle.get("sources", []) if s.get("id") in expected_ids]
    _require([s["id"] for s in sources] == expected_ids and sources == original, "contextual evidence changed after planning")
