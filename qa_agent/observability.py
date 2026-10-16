"""Bounded aggregate metrics and request logs without source bodies, queries or identities."""
import json
import logging
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

from starlette.responses import JSONResponse

BUCKETS = (0.1, 0.5, 1, 5, 30, 120, 600)


class Observability:
    def __init__(self, agent, cfg=None):
        self.agent, self.cfg = agent, cfg or {}
        self.started, self.lock, self.requests = time.monotonic(), threading.Lock(), {}
        self.min_free = int(self.cfg.get("minimum_free_bytes", 100_000_000))
        self.max_audit_age = int(agent.policy.get("audit_delivery", {}).get("max_age_s", 300))
        if not 1_000_000 <= self.min_free <= 1_000_000_000_000 or not 10 <= self.max_audit_age <= 3600:
            raise ValueError("invalid observability readiness limits")

    def observe(self, route, method, status, duration):
        with self.lock:
            # Only trusted route templates enter labels. Unknown paths share one bucket.
            key = (route, method, str(status // 100) + "xx")
            row = self.requests.setdefault(key, {"count": 0, "seconds": 0.0, "buckets": [0] * len(BUCKETS)})
            row["count"] += 1;row["seconds"] += duration
            for index, boundary in enumerate(BUCKETS):
                row["buckets"][index] += int(duration <= boundary)

    def health(self):
        checks, detail = {}, {}
        try:
            checks["database_readable"] = self.agent.store.q("SELECT 1 AS ok")[0]["ok"] == 1
            audit = self.agent.gateway.audit_delivery.health()
            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(audit["oldest"])).total_seconds()) if audit["oldest"] else 0
            checks["audit_backlog"] = self.agent.gateway.audit_delivery.admission_denial(self.agent.policy.get("audit_delivery")) is None
            detail.update(audit_projection_pending=audit["pending"], audit_oldest_pending_s=age,
                          active_runs=self.agent.store.q("SELECT COUNT(*) AS n FROM runs WHERE status IN ('planning','executing')")[0]["n"])
        except Exception:
            checks["database_readable"] = False
            checks["audit_backlog"] = False
        try:
            free = shutil.disk_usage(self.agent.store.data_dir).free
            checks["storage_free"] = free >= self.min_free
            detail["storage_free_bytes"] = free
        except OSError:
            checks["storage_free"] = False
        return {"ready": all(checks.values()), "scope": "process admission checks", "enterprise_qualified": False,
                "checks": checks, "observed": detail, "uptime_s": round(time.monotonic() - self.started, 3)}

    def prometheus(self):
        state = self.health()
        lines = ["# TYPE qa_process_ready gauge", f"qa_process_ready {int(state['ready'])}",
                 "# TYPE qa_service_uptime_seconds gauge", f"qa_service_uptime_seconds {state['uptime_s']}"]
        for key, value in state["observed"].items():
            name = "qa_" + key
            lines += [f"# TYPE {name} gauge", f"{name} {value}"]
        lines += ["# TYPE qa_http_requests_total counter", "# TYPE qa_http_request_duration_seconds histogram"]
        with self.lock:
            for (route, method, status), row in sorted(self.requests.items()):
                labels = f"route={json.dumps(route)},method={json.dumps(method)},status_class={json.dumps(status)}"
                lines.append(f"qa_http_requests_total{{{labels}}} {row['count']}")
                lines.append(f"qa_http_request_duration_seconds_sum{{{labels}}} {row['seconds']:.6f}")
                lines.append(f"qa_http_request_duration_seconds_count{{{labels}}} {row['count']}")
                for boundary, count in zip(BUCKETS, row["buckets"]):
                    lines.append(f'qa_http_request_duration_seconds_bucket{{{labels},le="{boundary}"}} {count}')
                lines.append(f'qa_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {row["count"]}')
        return "\n".join(lines) + "\n"


class RequestObservability:
    def __init__(self, app, *, observer):
        self.app, self.observer = app, observer
        self.logger = logging.getLogger("qa_agent.access")
        self.logger.setLevel(logging.INFO)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send);return
        request_id, started, status, sent = uuid.uuid4().hex, time.monotonic(), 500, False
        scope.setdefault("state", {})["request_id"] = request_id
        async def traced_send(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status, sent = message["status"], True
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode())]}
            await send(message)
        try:
            await self.app(scope, receive, traced_send)
        except Exception:
            if sent:
                raise
            await JSONResponse({"detail": "internal service error", "request_id": request_id}, 500)(scope, receive, traced_send)
        finally:
            duration = time.monotonic() - started
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope.get("method", "OTHER")
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                method = "OTHER"
            self.observer.observe(route, method, status, duration)
            self.logger.info(json.dumps({"event": "http_request", "request_id": request_id, "route": route,
                                         "method": method, "status": status, "duration_ms": round(duration * 1000, 3)}))
