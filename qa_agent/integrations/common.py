"""Helpers shared by tracker adapters: key/project validation, ADF flattening, acceptance-criteria extraction."""
import re
from .http import IntegrationError

PROJECT_RE = re.compile(r"[A-Z][A-Z0-9_]{1,19}")
KEY_RE = re.compile(r"([A-Z][A-Z0-9_]{1,19})-([A-Z0-9-]{1,30})")


class ProjectNotAllowed(IntegrationError):
    pass


def project_of(key: str) -> str:
    m = KEY_RE.fullmatch(key or "")
    if not m:
        raise ValueError(f"invalid issue key {key!r}")
    return m.group(1)


def check_project(project: str, allowlist) -> str:
    if not PROJECT_RE.fullmatch(project or ""):
        raise ValueError(f"invalid project key {project!r}")
    if project not in (allowlist or []):
        raise ProjectNotAllowed(f"project {project} is not in the project allowlist")
    return project


def adf_text(node) -> str:
    """Flatten Atlassian Document Format (Cloud v3) to plain text; strings pass through (DC / v2)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    inner = adf_text(node.get("content", []))
    if t in ("paragraph", "heading"):
        return inner + "\n\n"
    if t == "listItem":
        return inner.strip() + "\n"
    if t == "orderedList":
        items = [adf_text(c).strip() for c in node.get("content", [])]
        return "".join(f"{i + 1}. {x}\n" for i, x in enumerate(items)) + "\n"
    if t == "bulletList":
        return "".join(f"- {adf_text(c).strip()}\n" for c in node.get("content", [])) + "\n"
    return inner


_AC_HEAD = re.compile(r"(?im)^\s*(?:h\d\.\s*|#+\s*|\*)?acceptance criteria\s*:?\s*\*?\s*$")
_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*#]|AC\d+[:.)])\s+(.*\S)\s*$")


def extract_acceptance_criteria(fields: dict, ac_field: str | None = None) -> list[dict]:
    """Prefer a configured custom field; else the 'Acceptance criteria' section of the description."""
    src, text = "description", None
    if ac_field and fields.get(ac_field):
        src, text = ac_field, adf_text(fields[ac_field])
    else:
        desc = adf_text(fields.get("description"))
        m = _AC_HEAD.search(desc)
        text = desc[m.end():] if m else ""
    out = []
    for line in (text or "").splitlines():
        m = _ITEM.match(line)
        if m:
            out.append({"id": f"AC{len(out) + 1}", "text": m.group(1), "source": src})
        elif out and line.strip() and not line.startswith((" ", "\t")) and src == "description" and line.strip().endswith(":"):
            break
    return out


def attachment_meta(a: dict) -> dict:
    return {"id": str(a.get("id")), "filename": a.get("filename"), "mime_type": a.get("mimeType"),
            "size": a.get("size"), "created": a.get("created"), "author": (a.get("author") or {}).get("displayName")}
