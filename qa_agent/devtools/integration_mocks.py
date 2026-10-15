"""Local mock servers (stdlib, 127.0.0.1 only) for Jira Cloud/DC and Bitbucket Cloud/DC REST APIs.

Serves SYNTHETIC data only (fixtures/jira, fixtures/jira_extras, fixtures/repos). Records every request
(method, path, auth header presence) so tests can prove read-only behaviour, and supports fault injection
(`faults` = list of status codes returned for the next requests; `delay_s` to trigger timeouts).
Run standalone: python -m qa_agent.devtools.integration_mocks [port]"""
from __future__ import annotations
import json, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from .. import ROOT

EXPECTED_TOKEN = "synthetic-mock-token-not-a-secret"


def _adf(text):
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": ln}]}
                                                      for ln in text.split("\n") if ln.strip()]}


class MockState:
    def __init__(self, page_size_cap=2):
        self.requests, self.faults, self.delay_s, self.cap = [], [], 0.0, page_size_cap
        self.issues = {}
        for p in sorted((ROOT / "fixtures/jira").glob("*.json")):
            d = json.loads(p.read_text())
            ex = ROOT / "fixtures/jira_extras" / p.name
            d["_extra"] = json.loads(ex.read_text()) if ex.exists() else {}
            self.issues[d["key"]] = d
        # one synthetic issue in a NON-allowlisted project, to prove the allowlist holds
        self.issues["SECRET-1"] = {"key": "SECRET-1", "fields": {"summary": "synthetic other-project issue", "description": "x",
                                   "labels": [], "project": {"key": "SECRET"}, "status": {"name": "Open"}}, "_extra": {}}
        self.repo_root = ROOT / "fixtures/repos"


def make_handler(state: MockState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _rec(self):
            u = urlparse(self.path)
            state.requests.append({"method": self.command, "path": u.path, "query": parse_qs(u.query),
                                   "auth": bool(self.headers.get("Authorization"))})
            return u, {k: v[0] for k, v in parse_qs(u.query).items()}

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body).encode() if not isinstance(body, str) else body.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if code == 429:
                self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(data)

        def _mutation(self):
            self._rec()
            self._send(405, {"error": "mock is read-only"})
        do_POST = do_PUT = do_PATCH = do_DELETE = _mutation

        def do_GET(self):
            u, q = self._rec()
            if state.delay_s:
                time.sleep(state.delay_s)
            if state.faults:
                return self._send(state.faults.pop(0), {"error": "injected fault"})
            auth = self.headers.get("Authorization", "")
            if EXPECTED_TOKEN not in auth and "Basic " not in auth:
                return self._send(401, {"error": "unauthorized"})
            if auth.startswith("Basic "):
                import base64
                if base64.b64decode(auth[6:]).decode().split(":", 1)[-1] != EXPECTED_TOKEN:
                    return self._send(401, {"error": "unauthorized"})
            try:
                return self._route(unquote(u.path), q)
            except KeyError:
                return self._send(404, {"error": "not found"})

        # ---------------- routing ----------------
        def _route(self, path, q):
            m = re.fullmatch(r"/(?:jira/)?rest/api/(2|3)/issue/([^/]+)", path)
            if m:
                return self._send(200, self._issue(state.issues[m.group(2)], m.group(1) == "3"))
            m = re.fullmatch(r"/(?:jira/)?rest/api/(2|3)/issue/([^/]+)/comment", path)
            if m:
                cs = state.issues[m.group(2)]["_extra"].get("comments", [])
                s, n = int(q.get("startAt", 0)), min(int(q.get("maxResults", 50)), state.cap)
                page = [dict(c, body=_adf(c["body"]) if m.group(1) == "3" else c["body"]) for c in cs[s:s + n]]
                return self._send(200, {"startAt": s, "maxResults": n, "total": len(cs), "comments": page})
            m = re.fullmatch(r"/(?:jira/)?(?:rest/api/3/attachment/content/(\d+)|secure/attachment/(\d+)/.+)", path)
            if m:
                aid = m.group(1) or m.group(2)
                for i in state.issues.values():
                    for a in i["_extra"].get("attachments", []):
                        if a["id"] == aid:
                            return self._send(200, (ROOT / a["fixture"]).read_bytes(), a["mimeType"])
                raise KeyError(aid)
            if path in ("/rest/api/3/search/jql", "/jira/rest/api/2/search", "/rest/api/2/search"):
                return self._search(q, path.endswith("/jql"))
            if path.startswith("/2.0/"):
                return self._bb_cloud(path[5:], q)
            if path.startswith("/rest/api/1.0/projects/"):
                return self._bb_dc(path[len("/rest/api/1.0/projects/"):], q)
            raise KeyError(path)

        def _issue(self, d, v3):
            f = dict(d["fields"])
            f["project"] = f.get("project") or {"key": d["key"].split("-")[0]}
            if v3:
                f["description"] = _adf(f.get("description", ""))
            base = f"http://{self.headers.get('Host')}"
            f["attachment"] = [{"id": a["id"], "filename": a["filename"], "mimeType": a["mimeType"],
                                "size": (ROOT / a["fixture"]).stat().st_size, "content": f"{base}/attachment/{a['id']}"}
                               for a in d["_extra"].get("attachments", [])]
            return {"key": d["key"], "fields": f}

        def _search(self, q, cloud):
            mm = re.match(r'project = ([A-Z0-9_]+)(?: AND text ~ "((?:[^"\\]|\\.)*)")?', q.get("jql", ""))
            if not mm:
                return self._send(400, {"error": "bad jql"})
            keys = sorted(k for k in state.issues if k.startswith(mm.group(1) + "-"))
            if mm.group(2):
                t = mm.group(2).lower()
                keys = [k for k in keys if t in (state.issues[k]["fields"]["summary"] + state.issues[k]["fields"].get("description", "")).lower()]
            n = min(int(q.get("maxResults", 50)), state.cap)
            s = int(q.get("nextPageToken", 0) if cloud else q.get("startAt", 0))
            page = [self._issue(state.issues[k], cloud) for k in keys[s:s + n]]
            if cloud:
                nxt = s + n if s + n < len(keys) else None
                return self._send(200, {"issues": page, **({"nextPageToken": str(nxt)} if nxt else {"isLast": True})})
            return self._send(200, {"startAt": s, "maxResults": n, "total": len(keys), "issues": page})

        def _repo_dir(self, owner, slug):
            d = state.repo_root / owner / slug
            if not d.is_dir():
                raise KeyError(slug)
            return d

        def _bb_cloud(self, path, q):
            base = f"http://{self.headers.get('Host')}/2.0/"
            parts = path.strip("/").split("/")
            if parts[0] == "repositories" and len(parts) == 2:
                ws = parts[1]
                vals = [{"full_name": f"{ws}/{r.name}", "name": r.name, "mainbranch": {"name": "main"}, "is_private": True}
                        for r in sorted((state.repo_root / ws).iterdir())] if (state.repo_root / ws).is_dir() else []
                vals.append({"full_name": f"{ws}/not-allowlisted-repo", "name": "not-allowlisted-repo", "mainbranch": {"name": "main"}})
                return self._paged_cloud(vals, q, base + f"repositories/{ws}")
            if parts[0] == "repositories" and len(parts) == 3:
                self._repo_dir(parts[1], parts[2])
                return self._send(200, {"full_name": f"{parts[1]}/{parts[2]}", "name": parts[2], "mainbranch": {"name": "main"}, "is_private": True})
            if parts[0] == "repositories" and parts[3] == "src":
                d, rel = self._repo_dir(parts[1], parts[2]), "/".join(parts[5:])
                p = d / rel
                if p.is_file():
                    return self._send(200, p.read_bytes(), "text/plain")
                if p.is_dir():
                    vals = [{"path": c.relative_to(d).as_posix(), "type": "commit_directory" if c.is_dir() else "commit_file",
                             **({"size": c.stat().st_size} if c.is_file() else {})} for c in sorted(p.iterdir())]
                    return self._paged_cloud(vals, q, base + path.lstrip("/"))
                raise KeyError(rel)
            if parts[0] == "workspaces" and parts[2:] == ["search", "code"]:
                sq = q.get("search_query", "")
                repo, _, term = sq.partition(" ")
                d = self._repo_dir(parts[1], repo.split(":", 1)[1])
                vals = [{"file": {"path": f.relative_to(d).as_posix()}, "content_match_count": f.read_text().count(term)}
                        for f in sorted(d.rglob("*")) if f.is_file() and term in f.read_text()]
                return self._paged_cloud(vals, q, base + path.lstrip("/"))
            raise KeyError(path)

        def _paged_cloud(self, vals, q, url):
            page = int(q.get("page", 1)); n = state.cap
            out = {"values": vals[(page - 1) * n: page * n], "page": page, "pagelen": n, "size": len(vals)}
            if page * n < len(vals):
                extra = "&".join(f"{k}={v}" for k, v in q.items() if k not in ("page",))
                out["next"] = f"{url}?page={page + 1}" + (f"&{extra}" if extra else "")
            return self._send(200, out)

        def _bb_dc(self, path, q):
            parts = path.strip("/").split("/")
            s, n = int(q.get("start", 0)), state.cap
            def paged(vals):
                return {"values": vals[s:s + n], "start": s, "size": len(vals[s:s + n]), "isLastPage": s + n >= len(vals),
                        **({"nextPageStart": s + n} if s + n < len(vals) else {})}
            proj = parts[0]
            if parts[1:] == ["repos"]:
                vals = [{"slug": r.name, "name": r.name, "project": {"key": proj}, "public": False}
                        for r in sorted((state.repo_root / proj).iterdir())] if (state.repo_root / proj).is_dir() else []
                vals.append({"slug": "not-allowlisted-repo", "name": "x", "project": {"key": proj}})
                return self._send(200, paged(vals))
            slug = parts[2]
            d = self._repo_dir(proj, slug)
            if len(parts) == 3:
                return self._send(200, {"slug": slug, "name": slug, "project": {"key": proj}, "public": False})
            if parts[3] == "default-branch":
                return self._send(200, {"id": "refs/heads/main", "displayId": "main"})
            if parts[3] == "raw":
                p = d / "/".join(parts[4:])
                if not p.is_file():
                    raise KeyError(path)
                return self._send(200, p.read_bytes(), "text/plain")
            if parts[3] == "browse":
                rel = "/".join(parts[4:]); p = d / rel
                if not p.is_dir():
                    raise KeyError(path)
                vals = [{"path": {"components": [c.name]}, "type": "DIRECTORY" if c.is_dir() else "FILE",
                         **({"size": c.stat().st_size} if c.is_file() else {})} for c in sorted(p.iterdir())]
                return self._send(200, {"path": {"components": rel.split("/") if rel else []}, "children": paged(vals)})
            raise KeyError(path)
    return H


class MockServer:
    def __init__(self, port=0, page_size_cap=2):
        self.state = MockState(page_size_cap)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    import sys
    with MockServer(int(sys.argv[1]) if len(sys.argv) > 1 else 8701) as m:
        print(f"integration mocks on {m.url} (synthetic data; token {EXPECTED_TOKEN!r})", flush=True)
        threading.Event().wait()
