"""SyntheticGitProvider: read-only GitProvider over fixtures/repos/<OWNER>/<slug>/ (synthetic files only, no git, no network)."""
from pathlib import Path
from ..interfaces import GitProvider
from ..integrations.bitbucket import check_repo, check_ref, clean_path
from ..integrations.http import Unsupported


class SyntheticGitProvider(GitProvider):
    kind = "synthetic_git"

    def __init__(self, root: Path, repositories=None):
        self.root = Path(root).resolve()
        self.repos = list(repositories) if repositories is not None else sorted(
            f"{o.name}/{r.name}" for o in self.root.iterdir() if o.is_dir() for r in o.iterdir() if r.is_dir())

    def _dir(self, repo, path=""):
        owner, slug = check_repo(repo, self.repos)
        p = (self.root / owner / slug / clean_path(path)).resolve()
        if not str(p).startswith(str(self.root / owner / slug)):
            raise ValueError("path escapes repository")
        return p

    def list_repositories(self):
        return [self.get_repository(r) for r in self.repos]

    def get_repository(self, repo):
        self._dir(repo)
        return {"full_name": repo, "name": repo.split("/")[1], "default_branch": "main", "is_private": True, "provider": self.kind}

    def read_file(self, repo, path, ref="main"):
        check_ref(ref)
        p = self._dir(repo, path)
        if not p.is_file():
            raise KeyError(f"{path} not found in {repo}")
        return p.read_text()

    def list_dir(self, repo, path="", ref="main"):
        check_ref(ref)
        base, d = self._dir(repo), self._dir(repo, path)
        return [{"path": str(c.relative_to(base)), "type": "dir" if c.is_dir() else "file", "size": c.stat().st_size if c.is_file() else None}
                for c in sorted(d.iterdir())]

    def supports_code_search(self):
        return True

    def search_code(self, repo, query, max_results=25):
        if not query or len(query) > 200:
            raise ValueError("invalid code search query")
        base, out = self._dir(repo), []
        for f in sorted(base.rglob("*")):
            if f.is_file():
                n = f.read_text(errors="replace").count(query)
                if n:
                    out.append({"path": str(f.relative_to(base)), "matches": n})
        return out[:max_results]

    def repo_context(self, repo, ref=None):
        info = self.get_repository(repo)
        top = self.list_dir(repo, "", ref or "main")
        readme = next((e["path"] for e in top if e["path"].lower().startswith("readme")), None)
        return {"repository": info, "ref": ref or "main", "top_level": top,
                "readme": self.read_file(repo, readme)[:4000] if readme else None, "code_search_supported": True}
