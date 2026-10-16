"""Read-only Bitbucket adapters (Cloud REST 2.0, Data Center REST 1.0). Never clones; only GET.

Repository allowlist is enforced on every call ("WORKSPACE/repo" for Cloud, "PROJECT/repo" for DC).
Code search: Cloud uses /2.0/workspaces/{ws}/search/code only when code_search_enabled=true (it must be
enabled for the workspace); DC core REST has no code-search API, so it raises Unsupported."""
from __future__ import annotations
import posixpath, re
from urllib.parse import quote
from ..interfaces import GitProvider
from .http import ReadOnlyHttpClient, IntegrationError, Unsupported
from .pagination import CollectionBudget, IncompleteSource, cloud_next, next_offset
from ..util import sha256

SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


class RepoNotAllowed(IntegrationError):
    pass


def clean_path(path: str) -> str:
    if ".." in (path or "").replace("\\", "/").split("/") or "\x00" in (path or ""):
        raise ValueError(f"invalid path {path!r}")
    p = posixpath.normpath("/" + (path or "")).lstrip("/")
    if p in (".", ""):
        return ""
    if ".." in p.split("/") or "\\" in p or "\x00" in p:
        raise ValueError(f"invalid path {path!r}")
    return p


def check_repo(repo: str, allowlist) -> tuple[str, str]:
    owner, _, slug = (repo or "").partition("/")
    if not (SLUG.fullmatch(owner or "") and SLUG.fullmatch(slug or "")):
        raise ValueError(f"invalid repository {repo!r} (expected OWNER/slug)")
    if repo not in (allowlist or []):
        raise RepoNotAllowed(f"repository {repo} is not in the repository allowlist")
    return owner, slug


def check_ref(ref):
    if not REF.fullmatch(ref or "") or ".." in ref:
        raise ValueError(f"invalid ref {ref!r}")
    return ref


class _BitbucketBase(GitProvider):
    kind = "bitbucket"
    code_search_supported = False

    def __init__(self, cfg, gateway=None, run_id=None, transport=None, sleep=None):
        kw = {"sleep": sleep} if sleep else {}
        self.cfg = cfg
        self.repos = list(cfg.get("repositories") or [])
        self.http = ReadOnlyHttpClient(self.kind, cfg, gateway, run_id, transport, **kw)
        self.page_size = max(1, min(int(cfg.get("page_size", 50)), 100))
        self.max_pages = max(1, int(cfg.get("max_pages", 10)))
        self.max_file_bytes = int(cfg.get("max_file_bytes", 500_000))

    def repo_context(self, repo, ref=None):
        info = self.get_repository(repo)
        ref = ref or info.get("default_branch")
        if not ref:
            raise Unsupported("repository has no observed default branch; an explicit reviewed ref is required")
        top = self.list_dir(repo, "", ref)
        readme = next((e["path"] for e in top if e["type"] == "file" and e["path"].lower().startswith("readme")), None)
        body = self.read_file(repo, readme, ref) if readme else None
        return {"repository": info, "ref": ref, "resolved_commit": None, "version_basis": "requested ref; upstream commit not pinned",
                "top_level": top, "top_level_pagination_complete": True,
                "readme": body[:4000] if body is not None else None,
                "readme_complete": len(body) <= 4000 if body is not None else None,
                "readme_sha256": sha256(body) if body is not None else None,
                "code_search_supported": self.supports_code_search()}

    def supports_code_search(self):
        return False

    def _file_text(self, response):
        if len(response.content) > self.max_file_bytes:
            raise IntegrationError("file exceeds max_file_bytes")
        try:
            if b"\x00" in response.content:
                raise UnicodeError()
            return response.content.decode("utf-8")
        except UnicodeError:
            raise IncompleteSource("repository file is not supported UTF-8 text; source review required") from None

    def search_code(self, repo, query, max_results=25):
        raise Unsupported(f"{self.kind}: code search is not supported/enabled")


class BitbucketCloudProvider(_BitbucketBase):
    kind = "bitbucket_cloud"

    def _pages(self, path, params=None):
        original = dict(params or {}, pagelen=self.page_size)
        url, p, seen = path, original, set()
        budget = CollectionBudget(self.http, self.cfg)
        for _ in range(budget.max_pages):
            d = budget.get(url, p)
            rows = budget.rows(d, "values")
            url, p = cloud_next(self.http, path, d.get("next"), original), None
            if url in seen:
                raise IncompleteSource("Bitbucket collection repeats its continuation")
            if url:
                seen.add(url)
            yield rows
            if not url:
                return
        raise IncompleteSource("source collection exceeds page budget; retrieval is incomplete")

    def list_repositories(self):
        out = []
        for ws in sorted({r.split("/")[0] for r in self.repos}):
            for page in self._pages(f"2.0/repositories/{quote(ws)}"):
                out += [self._repo(v) for v in page if v.get("full_name") in self.repos]
        return out

    def _repo(self, v):
        return {"full_name": v.get("full_name"), "name": v.get("name"), "default_branch": (v.get("mainbranch") or {}).get("name"),
                "is_private": v.get("is_private"), "provider": self.kind}

    def get_repository(self, repo):
        ws, slug = check_repo(repo, self.repos)
        info = self._repo(self.http.get_json(f"2.0/repositories/{ws}/{slug}"))
        if info["full_name"] != repo:
            raise RepoNotAllowed("returned repository does not match the requested resource")
        return info

    def read_file(self, repo, path, ref="main"):
        ws, slug = check_repo(repo, self.repos)
        r = self.http.request("GET", f"2.0/repositories/{ws}/{slug}/src/{quote(check_ref(ref), safe='')}/{quote(clean_path(path))}", accept="*/*")
        return self._file_text(r)

    def list_dir(self, repo, path="", ref="main"):
        ws, slug = check_repo(repo, self.repos)
        p = clean_path(path)
        out = []
        for page in self._pages(f"2.0/repositories/{ws}/{slug}/src/{quote(check_ref(ref), safe='')}/{quote(p)}{'/' if p else ''}"):
            out += [{"path": v.get("path"), "type": "dir" if v.get("type") == "commit_directory" else "file", "size": v.get("size")} for v in page]
        return out

    def supports_code_search(self):
        return self.cfg.get("code_search_enabled") is True

    def search_code(self, repo, query, max_results=25):
        ws, slug = check_repo(repo, self.repos)
        if not self.supports_code_search():
            raise Unsupported(f"{self.kind}: code search disabled (set code_search_enabled=true once enabled for the workspace)")
        if not query or len(query) > 200 or '"' in query:
            raise ValueError("invalid code search query")
        out = []
        for page in self._pages(f"2.0/workspaces/{ws}/search/code", {"search_query": f"repo:{slug} {query}"}):
            for v in page:
                f = v.get("file") or {}
                out.append({"path": f.get("path"), "matches": v.get("content_match_count")})
            if len(out) >= max_results:
                break
        return out[:max_results]


class BitbucketDataCenterProvider(_BitbucketBase):
    kind = "bitbucket_dc"

    def _pages(self, path, params=None):
        start = 0
        budget = CollectionBudget(self.http, self.cfg)
        for _ in range(budget.max_pages):
            d = budget.get(path, dict(params or {}, start=start, limit=self.page_size))
            budget.rows(d, "values")
            continuation = next_offset(d, start)
            yield d
            if continuation is None:
                return
            start = continuation
        raise IncompleteSource("source collection exceeds page budget; retrieval is incomplete")

    def _repo(self, v):
        return {"full_name": f"{(v.get('project') or {}).get('key')}/{v.get('slug')}", "name": v.get("name"),
                "default_branch": v.get("defaultBranch"), "is_private": not v.get("public", False), "provider": self.kind}

    def list_repositories(self):
        out = []
        for proj in sorted({r.split("/")[0] for r in self.repos}):
            for d in self._pages(f"rest/api/1.0/projects/{quote(proj)}/repos"):
                out += [r for r in (self._repo(v) for v in d.get("values") or []) if r["full_name"] in self.repos]
        return out

    def get_repository(self, repo):
        proj, slug = check_repo(repo, self.repos)
        info = self._repo(self.http.get_json(f"rest/api/1.0/projects/{proj}/repos/{slug}"))
        if info["full_name"] != repo:
            raise RepoNotAllowed("returned repository does not match the requested resource")
        try:
            info["default_branch"] = self.http.get_json(f"rest/api/1.0/projects/{proj}/repos/{slug}/default-branch").get("displayId")
        except IntegrationError as exc:
            if exc.status != 404:
                raise
        return info

    def read_file(self, repo, path, ref="main"):
        proj, slug = check_repo(repo, self.repos)
        r = self.http.request("GET", f"rest/api/1.0/projects/{proj}/repos/{slug}/raw/{quote(clean_path(path))}",
                              {"at": check_ref(ref)}, accept="*/*")
        return self._file_text(r)

    def list_dir(self, repo, path="", ref="main"):
        proj, slug = check_repo(repo, self.repos)
        p = clean_path(path)
        out, start = [], 0
        budget = CollectionBudget(self.http, self.cfg)
        for _ in range(budget.max_pages):
            d = budget.get(f"rest/api/1.0/projects/{proj}/repos/{slug}/browse/{quote(p)}",
                                   {"at": check_ref(ref), "start": start, "limit": self.page_size})
            ch = d.get("children") or {}
            for v in budget.rows(ch, "values"):
                name = "/".join((v.get("path") or {}).get("components") or [])
                out.append({"path": f"{p}/{name}" if p else name, "type": "dir" if v.get("type") == "DIRECTORY" else "file", "size": v.get("size")})
            continuation = next_offset(ch, start)
            if continuation is None:
                return out
            start = continuation
        raise IncompleteSource("source directory exceeds page budget; retrieval is incomplete")
