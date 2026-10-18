"""Caller/project policy independent of the connector's broader service credential."""
import hashlib
import json

from .auth import Principal, ROLES


class AccessPolicy:
    def __init__(self, projects, cfg=None, *, shared=False):
        self.projects = projects
        self.cfg = cfg or {}
        self.strict = shared or self.cfg.get("mode") == "project"
        self.grants = self.cfg.get("group_grants", {})
        for group, grant in self.grants.items():
            if not isinstance(group, str) or not isinstance(grant, dict) or not grant.get("tenant_id"):
                raise ValueError("invalid identity group grant")
            for project, role in grant.get("projects", {}).items():
                pc = projects.get(project)
                if not pc or pc.get("tenant_id", "local") != grant["tenant_id"] or role not in ROLES:
                    raise ValueError("identity group grant refers to unknown tenant/project/role")

    @property
    def revision(self):
        return hashlib.sha256(json.dumps({"projects": self.projects, "policy": self.cfg}, sort_keys=True).encode()).hexdigest()

    def role(self, principal, project):
        pc = self.projects.get(project)
        if pc is None:
            return None
        if principal.mode == "demo" and not self.strict:
            return "approver"
        if principal.tenant_id != pc.get("tenant_id", "local"):
            return None
        if principal.project_roles is None:
            return principal.role if not self.strict else None
        granted = principal.project_roles.get(project)
        if granted not in ROLES:
            return None
        return min((granted, principal.role), key=lambda role: ROLES.get(role, 0))

    def allows(self, principal, project, role="viewer"):
        return ROLES.get(self.role(principal, project), 0) >= ROLES[role]

    def principal(self, claims):
        groups = claims.get(self.cfg.get("group_claim", "groups"), [])
        if not isinstance(groups, list) or len(groups) > 1024 or any(not isinstance(g, str) or len(g) > 256 for g in groups):
            raise ValueError("invalid verified identity groups")
        selected = [self.grants[g] for g in groups if g in self.grants]
        tenants = {g["tenant_id"] for g in selected}
        if len(tenants) > 1:
            raise ValueError("identity has ambiguous organization grants")
        project_roles = {}
        for grant in selected:
            for project, role in grant.get("projects", {}).items():
                if ROLES[role] > ROLES.get(project_roles.get(project), 0):
                    project_roles[project] = role
        role = max(project_roles.values(), key=lambda r: ROLES[r]) if project_roles else "viewer"
        identity = "oidc:" + hashlib.sha256((claims["iss"] + "\0" + claims["sub"]).encode()).hexdigest()
        return Principal(identity, role, "oidc", next(iter(tenants), "unassigned"), project_roles,
                         str(claims.get("name") or claims.get("preferred_username") or "Organization user")[:200],
                         authorization_revision=self.revision)
