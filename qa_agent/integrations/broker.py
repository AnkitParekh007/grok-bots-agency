"""Operator-configured token/MCP connections and human-reviewed resource selection.

Discovery is an authenticated admin action, separate from permission to read content
or send it to a model. Projects use narrowed adapters, never the credential's full ACL.
"""
import copy
import json
import re
import time
from urllib.parse import quote, urlsplit

from .http import ReadOnlyHttpClient, IntegrationError, IntegrationNotAuthorized
from .jira import JiraCloudProvider, JiraDataCenterProvider
from .bitbucket import BitbucketCloudProvider, BitbucketDataCenterProvider, SLUG
from .common import PROJECT_RE
from .mcp import RemoteMcpClient
from .mcp_adapters import McpJiraProvider, McpBitbucketProvider, complete
from ..auth import has_role
from ..util import canon, sha256, now

PROVIDERS = {"jira_cloud": JiraCloudProvider, "jira_dc": JiraDataCenterProvider,
             "bitbucket_cloud": BitbucketCloudProvider, "bitbucket_dc": BitbucketDataCenterProvider}


class ConnectionBroker:
    def __init__(self, cfg, store, gateway, projects, *, transports=None, clock=time.time):
        self.cfg, self.store, self.gateway, self.projects = cfg or {}, store, gateway, projects
        self.transports, self.clock = transports or {}, clock
        self.mcp_clients, self.adapters = {}, {}
        for name, conn in self.cfg.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or conn.get("provider") not in PROVIDERS or conn.get("route") not in ("token", "mcp") or not conn.get("tenant_id"):
                raise ValueError("invalid operator connection definition")
        store.x("CREATE TABLE IF NOT EXISTS connection_discovery(connection_id TEXT, receipt TEXT PRIMARY KEY, config_hash TEXT, "
                "actor TEXT, resources TEXT, expires REAL, complete INTEGER)")
        store.x("CREATE TABLE IF NOT EXISTS connection_scopes(connection_id TEXT PRIMARY KEY, config_hash TEXT, resources TEXT, "
                "approved_by TEXT, approved_at TEXT, revision TEXT)")

    def close(self):
        for client in self.mcp_clients.values():
            client.close()
        for adapter in self.adapters.values():
            if hasattr(adapter, "http"):
                adapter.http._client.close()

    def _connection(self, name):
        conn = self.cfg.get(name)
        if not conn:
            raise IntegrationNotAuthorized("connection not found", 404)
        return conn

    def _admin(self, name, principal):
        conn = self._connection(name)
        if not principal.authenticated or principal.kind != "human" or not has_role(principal, "approver") or \
                principal.tenant_id != conn["tenant_id"] or principal.name not in conn.get("administrators", []):
            self.gateway.record(None, "connection.admin", {"connection": name, "user": principal.name}, "deny", "connection administrator scope denied")
            raise IntegrationNotAuthorized("connection not found", 404)
        return conn

    def _hash(self, name):
        return sha256(canon(self._connection(name)))

    def _settings(self, name, *, discovery=False):
        conn = self._connection(name)
        if conn.get("enabled") is not True or conn.get("approved") is not True or not conn.get("approval_ref"):
            raise IntegrationNotAuthorized("connection is disabled or lacks operator approval")
        if discovery and conn.get("discovery_approved") is not True:
            raise IntegrationNotAuthorized("resource discovery is not approved")
        settings = copy.deepcopy(conn["http"] if conn["route"] == "token" else conn["mcp"])
        settings.update(enabled=True, approved=True, approval_ref=conn["approval_ref"])
        if discovery:
            settings.update(discovery_only=True, discovery_approved=True)
            settings["timeout_s"] = min(float(settings.get("timeout_s", 10)), 10)
            settings["max_retries"] = min(int(settings.get("max_retries", 1)), 1)
            settings["max_backoff_s"] = min(float(settings.get("max_backoff_s", 2)), 2)
            settings["max_response_bytes"] = min(int(settings.get("max_response_bytes", 2_000_000)), 2_000_000)
        return settings

    def _mcp(self, name, *, discovery=False):
        fingerprint = self._hash(name)
        key = (name, fingerprint)
        if key not in self.mcp_clients:
            self.mcp_clients[key] = RemoteMcpClient(self._settings(name, discovery=discovery), self.gateway,
                                                   transport=self.transports.get(name))
        return self.mcp_clients[key]

    def list(self, principal):
        visible = []
        for name, conn in self.cfg.items():
            if principal.tenant_id != conn["tenant_id"] or principal.name not in conn.get("administrators", []) or not principal.authenticated or principal.kind != "human" or not has_role(principal, "approver"):
                continue
            scope = self._scope(name)
            visible.append({"id": name, "provider": conn["provider"], "route": conn["route"], "label": conn.get("label", name),
                            "enabled": conn.get("enabled") is True, "discovery_approved": conn.get("discovery_approved") is True,
                            "selected_resources": scope.get("resources", []), "revision": scope.get("revision"),
                            "target": conn.get("display_target", "Operator-configured destination")})
        return visible

    def discover(self, name, principal):
        conn = self._admin(name, principal)
        settings = self._settings(name, discovery=True)
        reason = self.gateway.check(None, "connection.discover", {"connection": name}, None)
        if reason:
            raise IntegrationNotAuthorized("connection discovery denied by policy")
        rows, done = [], False
        try:
            if conn["route"] == "mcp":
                payload = self._mcp(name, discovery=True).read("resource.discover", {})
                rows = complete(payload, "resources")
                done = True
            else:
                http = ReadOnlyHttpClient(conn["provider"], settings, self.gateway, transport=self.transports.get(name))
                try:
                    rows, done = self._discover_rest(conn, http)
                finally:
                    http._client.close()
            if len(rows) > 5000:
                raise IntegrationError("resource discovery exceeds item limit")
            ids, resources = set(), []
            for row in rows:
                key = row.get("id")
                if not self._valid_resource(conn, key) or key in ids:
                    raise IntegrationError("discovery returned an invalid, repeated or out-of-scope resource")
                ids.add(key)
                resources.append({"id": key, "name": str(row.get("name") or key)[:200]})
            receipt = sha256(canon({"connection": name, "config": self._hash(name), "actor": principal.name,
                                    "time": self.clock(), "resources": resources, "complete": done}))
            self.store.x("DELETE FROM connection_discovery WHERE expires<?", (self.clock(),))
            self.store.x("INSERT INTO connection_discovery VALUES(?,?,?,?,?,?,?)",
                         (name, receipt, self._hash(name), principal.name, canon(resources), self.clock() + 600, int(done)))
            self.gateway.record(None, "connection.discover", {"connection": name, "user": principal.name, "receipt": receipt},
                                "allow", "bounded authenticated discovery", "complete" if done else "incomplete")
            return {"connection": name, "resources": resources, "complete": done, "receipt": receipt,
                    "expires_in_s": 600, "scope_revision": self._scope(name).get("revision")}
        except IntegrationError:
            self.gateway.record(None, "connection.discover", {"connection": name, "user": principal.name}, "deny", "discovery failed")
            raise

    def _valid_resource(self, conn, key):
        if not isinstance(key, str):
            return False
        if conn["provider"].startswith("jira"):
            return bool(PROJECT_RE.fullmatch(key))
        owner, _, slug = key.partition("/")
        return bool(SLUG.fullmatch(owner) and SLUG.fullmatch(slug) and owner in conn.get("discovery", {}).get("owners", []))

    def _discover_rest(self, conn, http):
        provider = conn["provider"]
        pages = max(1, min(int(conn.get("discovery", {}).get("max_pages", 10)), 50))
        rows = []
        deadline, requests = time.monotonic() + 60, 0
        def budget():
            nonlocal requests
            requests += 1
            if requests > pages or len(rows) > 5000 or time.monotonic() > deadline:
                raise IntegrationError("discovery exceeded request/item/deadline limit; narrow the approved scope")
        if provider == "jira_dc":
            data = http.get_json("rest/api/2/project")
            if not isinstance(data, list):
                raise IntegrationError("Jira project discovery contract mismatch")
            return [{"id": r.get("key"), "name": r.get("name")} for r in data], True
        if provider == "jira_cloud":
            start = 0
            for _ in range(pages):
                budget()
                data = http.get_json("rest/api/3/project/search", {"startAt": start, "maxResults": 100})
                values = data.get("values")
                if not isinstance(values, list):
                    raise IntegrationError("Jira project discovery contract mismatch")
                rows += [{"id": r.get("key"), "name": r.get("name")} for r in values]
                start += len(values)
                if data.get("isLast") is True or (isinstance(data.get("total"), int) and start >= data["total"]):
                    return rows, True
                if not values:
                    raise IntegrationError("Jira discovery stopped without a completeness marker")
            return rows, False
        owners = conn.get("discovery", {}).get("owners", [])
        if not owners or len(owners) > 20 or any(not isinstance(o, str) or not SLUG.fullmatch(o) for o in owners):
            raise IntegrationNotAuthorized("repository discovery requires explicit approved workspaces/projects")
        all_done = True
        for owner in owners:
            url, start, seen = (f"2.0/repositories/{quote(owner)}" if provider == "bitbucket_cloud" else f"rest/api/1.0/projects/{quote(owner)}/repos"), 0, set()
            owner_done = False
            for _ in range(pages):
                budget()
                data = http.get_json(url, {"pagelen": 100} if provider == "bitbucket_cloud" and not seen else
                                     ({"start": start, "limit": 100} if provider == "bitbucket_dc" else None))
                values = data.get("values")
                if not isinstance(values, list):
                    raise IntegrationError("repository discovery contract mismatch")
                rows += [{"id": r.get("full_name") if provider == "bitbucket_cloud" else f"{(r.get('project') or {}).get('key')}/{r.get('slug')}",
                          "name": r.get("name")} for r in values]
                if provider == "bitbucket_cloud":
                    next_url = data.get("next")
                    if not next_url:
                        owner_done = True;break
                    if not isinstance(next_url, str) or next_url in seen:
                        raise IntegrationError("invalid discovery pagination cursor")
                    if urlsplit(next_url).path != urlsplit(http.url(f"2.0/repositories/{quote(owner)}")).path:
                        raise IntegrationNotAuthorized("discovery pagination escaped its approved workspace endpoint")
                    seen.add(next_url);url = next_url
                else:
                    if data.get("isLastPage") is True:
                        owner_done = True;break
                    next_start = data.get("nextPageStart")
                    if type(next_start) is not int or next_start <= start:
                        raise IntegrationError("invalid discovery pagination cursor")
                    start = next_start
            all_done = all_done and owner_done
        return rows, all_done

    def select(self, name, principal, receipt, resources, previous_revision=None):
        conn = self._admin(name, principal)
        self._settings(name, discovery=True)
        if not isinstance(resources, list) or not resources or len(resources) > 500 or any(not self._valid_resource(conn, r) for r in resources) or len(set(resources)) != len(resources):
            raise IntegrationNotAuthorized("select between 1 and 500 distinct discovered resources")
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.store.db.execute("SELECT * FROM connection_discovery WHERE receipt=? AND connection_id=?", (receipt, name)).fetchone()
                scope = self._scope(name)
                if scope.get("revision") != previous_revision:
                    raise IntegrationError("connection scope changed; rediscover and review again", 409)
                if not row or row["actor"] != principal.name or row["expires"] <= self.clock() or row["config_hash"] != self._hash(name):
                    raise IntegrationNotAuthorized("discovery receipt is expired, changed or belongs to another administrator")
                if not row["complete"]:
                    raise IntegrationNotAuthorized("incomplete discovery cannot approve a resource scope")
                discovered = {r["id"] for r in json.loads(row["resources"])}
                if not set(resources).issubset(discovered):
                    raise IntegrationNotAuthorized("selected resources were not in this authenticated discovery")
                record = {"connection": name, "resources": sorted(resources), "approved_by": principal.name,
                          "approved_at": now(), "config_hash": self._hash(name)}
                revision = sha256(canon(record))
                self.store.db.execute("INSERT INTO connection_scopes VALUES(?,?,?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET "
                                      "config_hash=excluded.config_hash,resources=excluded.resources,approved_by=excluded.approved_by,"
                                      "approved_at=excluded.approved_at,revision=excluded.revision",
                                      (name, record["config_hash"], canon(record["resources"]), principal.name, record["approved_at"], revision))
                self.store.db.execute("DELETE FROM connection_discovery WHERE receipt=?", (receipt,))
                self.store.db.commit()
            except Exception:
                self.store.db.rollback();raise
        self.gateway.record(None, "connection.scope", {"connection": name, "user": principal.name, "resources": sorted(resources), "revision": revision},
                            "allow", "human connection administrator selected discovered resources")
        return {"connection": name, "resources": sorted(resources), "revision": revision}

    def revoke(self, name, principal, previous_revision):
        self._admin(name, principal)
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                current = self._scope(name)
                if not current or current.get("revision") != previous_revision:
                    raise IntegrationError("connection scope changed; reload before revoking", 409)
                self.store.db.execute("DELETE FROM connection_scopes WHERE connection_id=?", (name,))
                self.store.db.commit()
            except Exception:
                self.store.db.rollback();raise
        self.gateway.record(None, "connection.scope", {"connection": name, "user": principal.name, "revision": previous_revision},
                            "allow", "human connection administrator revoked resource scope")
        return {"connection": name, "revoked": True}

    def _scope(self, name):
        rows = self.store.q("SELECT * FROM connection_scopes WHERE connection_id=?", (name,))
        if not rows or rows[0]["config_hash"] != self._hash(name):
            return {}
        return {**rows[0], "resources": json.loads(rows[0]["resources"])}

    def revision(self, project):
        pc = self.projects.get(project, {})
        return {kind: {"config": self._hash(pc[kind + "_connection"]), "selection": self._scope(pc[kind + "_connection"]).get("revision")}
                for kind in ("tracker", "git") if pc.get(kind + "_connection")}

    def adapter(self, project, kind):
        pc = self.projects[project]
        name = pc[kind + "_connection"]
        conn = self._connection(name)
        if conn["tenant_id"] != pc.get("tenant_id", "local") or (conn["provider"].startswith("jira") != (kind == "tracker")):
            raise IntegrationNotAuthorized("connection tenant/provider does not match project")
        scope = self._scope(name)
        resources = [pc["jira_project"]] if kind == "tracker" else pc.get("repositories", [])
        if not resources or not set(resources).issubset(scope.get("resources", [])):
            raise IntegrationNotAuthorized("project resources lack an active reviewed connection scope")
        fingerprint = sha256(canon({"connection": self._hash(name), "scope": scope["revision"], "project": pc}))
        key = (name, project, fingerprint)
        if key not in self.adapters:
            settings = self._settings(name)
            settings.update(projects=resources if kind == "tracker" else [], repositories=resources if kind == "git" else [])
            if conn["route"] == "token":
                self.adapters[key] = PROVIDERS[conn["provider"]](settings, self.gateway, transport=self.transports.get(name))
            else:
                client = self._mcp(name, discovery=True)
                self.adapters[key] = (McpJiraProvider if kind == "tracker" else McpBitbucketProvider)(settings, client)
        return self.adapters[key]
