"""Regression tests for PR #1 review findings (egress, retry/concurrent budget, run-level access)."""
import json, socket, threading
import httpx, pytest
from fastapi.testclient import TestClient
from qa_agent import ROOT
from qa_agent.gateway import PolicyDenied
from qa_agent.providers.models import OpenAICompatibleProvider, ProviderConfigError
from qa_agent.runtime import QAAgent
from qa_agent.util import load_json
from tests.b1_helpers import compatible_cfg, fake_key

CTX = {"story_key": "PCC-DEMO-2", "data_class": "synthetic", "story": load_json(ROOT / "fixtures/jira/PCC-DEMO-2.json"), "catalog": {}}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect
    def guard(self, addr):
        if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"external network attempted: {addr}")
        return real(self, addr)
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture
def key(monkeypatch):
    k = fake_key(); monkeypatch.setenv("MODEL_API_KEY", k); return k


def ok_body():
    b = load_json(ROOT / "fixtures/model_recordings/chat_completion_ok.json"); b["choices"][0]["message"]["content"] = "{}"; return b


class Script:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def __call__(self, req):
        self.calls.append(req)
        st, body = self.items.pop(0)
        return httpx.Response(st, json=body)


APPROVED = dict(approved=True, approval_ref="TEST-ONLY")


# ---------------- egress: https + policy-owned allowlist ----------------
@pytest.mark.provider
@pytest.mark.security
def test_non_loopback_http_base_url_rejected_at_construction(key):
    with pytest.raises(ProviderConfigError, match="https"):
        OpenAICompatibleProvider(compatible_cfg("http://api.groq.com/v1", **APPROVED))
    OpenAICompatibleProvider(compatible_cfg("http://127.0.0.1:9/v1"))      # loopback stub may use http


@pytest.mark.provider
@pytest.mark.security
def test_gateway_denies_http_even_if_provider_check_bypassed(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((200, ok_body()))
    p = OpenAICompatibleProvider(compatible_cfg("https://api.groq.com/v1", **APPROVED), gateway=a.gateway, transport=httpx.MockTransport(s), sleep=lambda x: None)
    p.base_url = "http://api.groq.com/v1"                    # downgrade after construction
    with pytest.raises(PolicyDenied, match="must use https"):
        p.generate("plan_story", CTX, {}, run_id="r-http")
    assert s.calls == [] and a.store.audit_rows("r-http")[-1]["decision"] == "deny"


@pytest.mark.provider
@pytest.mark.security
def test_provider_config_cannot_extend_host_allowlist(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((200, ok_body()))
    cfg = compatible_cfg("https://evil.example/v1", **APPROVED, approved_model_hosts=["evil.example"])   # ignored
    p = OpenAICompatibleProvider(cfg, gateway=a.gateway, transport=httpx.MockTransport(s), sleep=lambda x: None)
    with pytest.raises(PolicyDenied, match="not in policy approved_hosts"):
        p.generate("plan_story", CTX, {}, run_id="r-host")
    assert s.calls == []
    assert load_json(ROOT / "config/policy.json")["tools"]["model.request"]["approved_hosts"] == []
    assert "approved_model_hosts" not in load_json(ROOT / "config/agent.json")["providers"]["compatible"]


# ---------------- budget: per-attempt reservation, concurrency, missing usage ----------------
from qa_agent.runtime import _BudgetLedger, BudgetExceeded
from qa_agent.store import Store


def compatible(a, s, **over):
    return OpenAICompatibleProvider(compatible_cfg("http://127.0.0.1:9/v1", **over), gateway=a.gateway, transport=httpx.MockTransport(s), sleep=lambda x: None)


def ledger(a, run_id="r-b"):
    return _BudgetLedger(a, run_id, "compatible", "contract-test", "plan_story", 0)


@pytest.mark.cost
def test_every_retry_attempt_is_reserved_and_charged(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((503, {}), (429, {}), (200, ok_body()))
    p = compatible(a, s)
    r = p.generate("plan_story", CTX, {}, run_id="r-b", budget=ledger(a))
    rows = a.store.run_usage("r-b")["calls"]
    assert [x["outcome"] for x in rows] == ["http_503", "http_429", "stop"] and [x["http_attempt"] for x in rows] == [0, 1, 2]
    est = rows[0]["est_usd"]
    assert rows[0]["cost_usd"] == rows[1]["cost_usd"] == est > 0 and rows[0]["prompt_tokens"] is None   # unknown -> conservative
    assert rows[2]["cost_usd"] == r.cost_usd and rows[2]["priced"] == 1 and rows[2]["total_tokens"] > 0
    assert a.store.run_spend("r-b") == pytest.approx(2 * est + r.cost_usd)


@pytest.mark.cost
def test_retries_stop_when_next_attempt_would_exceed_cap(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((503, {}), (503, {}), (200, ok_body()))
    p = compatible(a, s)
    est = p.estimate_max_cost(len(json.dumps(p.build_body("plan_story", CTX, {})["messages"])))
    a.budget["per_run_usd"] = est * 1.5                   # room for exactly one worst-case attempt
    with pytest.raises(BudgetExceeded, match="per-run cap"):
        p.generate("plan_story", CTX, {}, run_id="r-b", budget=ledger(a))
    assert len(s.calls) == 1                              # the retry was never sent
    assert any(r["tool"] == "model.budget_gate" and r["decision"] == "deny" for r in a.store.audit_rows("r-b"))


@pytest.mark.cost
def test_first_attempt_denied_sends_nothing(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((200, ok_body()))
    a.budget["daily_usd"] = 1e-9
    with pytest.raises(BudgetExceeded, match="daily cap"):
        compatible(a, s).generate("plan_story", CTX, {}, run_id="r-b", budget=ledger(a))
    assert s.calls == [] and a.store.run_usage("r-b")["calls"] == []


@pytest.mark.cost
def test_missing_usage_keeps_conservative_charge_and_null_tokens(tmp_path, key):
    a = QAAgent(tmp_path); b = ok_body(); b.pop("usage")
    with pytest.raises(Exception, match="usage"):
        compatible(a, Script((200, b))).generate("plan_story", CTX, {}, run_id="r-b", budget=ledger(a))
    (row,) = a.store.run_usage("r-b")["calls"]
    assert row["outcome"] == "usage_missing" and row["priced"] == 0 and row["cost_usd"] == row["est_usd"] > 0
    assert all(row[k] is None for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"))


@pytest.mark.cost
@pytest.mark.security
def test_policy_denied_attempt_not_charged(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((200, ok_body()))
    p = compatible(a, s); p.base_url = "https://evil.example/v1"
    with pytest.raises(PolicyDenied):
        p.generate("plan_story", CTX, {}, run_id="r-b", budget=ledger(a))
    (row,) = a.store.run_usage("r-b")["calls"]
    assert row["outcome"] == "denied_not_sent" and row["cost_usd"] == 0 and s.calls == []


@pytest.mark.cost
@pytest.mark.parametrize("separate_connections", [False, True])
def test_concurrent_reservations_are_atomic(tmp_path, separate_connections):
    Store(tmp_path)   # create schema
    stores = [Store(tmp_path) if separate_connections else None for _ in range(12)]
    shared = Store(tmp_path)
    ok, start = [], threading.Barrier(12)

    def worker(i):
        st = stores[i] or shared
        start.wait()
        uid, reason = st.reserve_spend(f"run-{i}", "compatible", "m", "plan_story", 0, 0, 1.0, per_run_cap=100.0, daily_cap=5.0)
        if uid:
            ok.append(i)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(ok) == 5 and shared.day_spend() == pytest.approx(5.0)   # never over the daily cap


@pytest.mark.cost
@pytest.mark.integration
def test_concurrent_runs_share_daily_cap(tmp_path, key):
    """Two agents (separate Store connections, same DB) planning concurrently cannot jointly exceed the daily cap."""
    from tests.b1_helpers import agent_cfg, model_stub
    with model_stub() as stub:
        probe = QAAgent(tmp_path / "probe", agent_cfg=agent_cfg("compatible", stub.base))
        est = probe.store.run_usage(probe.start_run("Plan PCC-DEMO-2", wait=True))["calls"][0]["est_usd"]
        cfg = agent_cfg("compatible", stub.base, budget={"per_run_usd": 10, "daily_usd": est * 1.5})   # room for one attempt
        stub.queue([{"sleep": 1}, {"sleep": 1}])   # both requests in flight at the same time (reservations held)
        agents = [QAAgent(tmp_path / "shared", agent_cfg=cfg) for _ in range(2)]
        rids, start = [], threading.Barrier(2)

        def go(ag):
            start.wait(); rids.append(ag.start_run("Plan PCC-DEMO-2", wait=True))
        ts = [threading.Thread(target=go, args=(ag,)) for ag in agents]
        [t.start() for t in ts]; [t.join() for t in ts]
        st = agents[0].store
        statuses = sorted(st.get_run(r)["status"] for r in rids)
        assert statuses == ["awaiting_approval", "failed"]
        failed = next(r for r in rids if st.get_run(r)["status"] == "failed")
        assert "daily cap" in st.get_run(failed)["error"]
        assert st.day_spend() <= est * 1.5 + 1e-12


# ---------------- run-level access in authenticated (local) mode ----------------
from qa_agent.auth import UserStore
from qa_agent.server import create_app
from tests.b1_helpers import wait_status

PROMPT = "Read the synthetic supplier import story, generate test cases, execute the approved tests, and give me the results."


@pytest.fixture
def local(tmp_path):
    us = UserStore(tmp_path / "u.json")
    t = {n: us.add(n, r) for n, r in [("rita", "requester"), ("ron", "requester"), ("vic", "viewer"), ("bob", "approver")]}
    app = create_app(tmp_path / "d", auth_mode="local", users_file=tmp_path / "u.json")
    return TestClient(app), t


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.approval
@pytest.mark.security
def test_run_level_access_local_mode(local):
    c, t = local
    rid = c.post("/api/chat", json={"message": PROMPT}, headers=H(t["rita"])).json()["run_id"]
    run = wait_status(lambda: c.get(f"/api/runs/{rid}", headers=H(t["rita"])).json(), {"awaiting_approval"})
    for who in ("ron", "vic"):                                    # not owner, not approver
        for path in ("", "/usage", "/report.json", "/report.html", "/audit.jsonl", "/artifacts/report.json"):
            assert c.get(f"/api/runs/{rid}{path}", headers=H(t[who])).status_code == 404, (who, path)
        assert rid not in [r["id"] for r in c.get("/api/runs", headers=H(t[who])).json()]
    assert c.post(f"/api/runs/{rid}/cancel", headers=H(t["ron"])).status_code == 403
    assert c.get("/api/audit/export", headers=H(t["vic"])).status_code == 403          # cross-run data: approver only
    assert c.get("/api/telemetry", headers=H(t["rita"])).status_code == 403
    assert c.get("/api/audit/export", headers=H(t["bob"])).status_code == 200
    # owner and approver keep access; approver can approve
    assert rid in [r["id"] for r in c.get("/api/runs", headers=H(t["rita"])).json()]
    assert c.get(f"/api/runs/{rid}", headers=H(t["bob"])).status_code == 200
    r = c.post(f"/api/runs/{rid}/approval", json={"decision": "approved", "plan_hash": run["plan_hash"]}, headers=H(t["bob"]))
    assert r.status_code == 200
    wait_status(lambda: c.get(f"/api/runs/{rid}", headers=H(t["rita"])).json(), {"completed"})
    assert c.get(f"/api/runs/{rid}/report.json", headers=H(t["rita"])).status_code == 200
    # denials are audited
    rows = c.get(f"/api/runs/{rid}/audit.jsonl", headers=H(t["bob"])).text
    assert "http.run_access" in rows


@pytest.mark.approval
def test_demo_mode_is_single_user_no_run_checks(tmp_path):
    c = TestClient(create_app(tmp_path))
    rid = c.post("/api/chat", json={"message": PROMPT}).json()["run_id"]
    wait_status(lambda: c.get(f"/api/runs/{rid}").json(), {"awaiting_approval"})
    assert c.get(f"/api/runs/{rid}/usage").status_code == 200 and rid in [r["id"] for r in c.get("/api/runs").json()]
