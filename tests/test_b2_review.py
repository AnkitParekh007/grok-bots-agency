"""Regression tests for PR #2 review findings. No live model calls."""
import copy, json, socket, threading
from datetime import datetime, timedelta, timezone
import httpx, pytest
from qa_agent import ROOT
from qa_agent.providers.models import GrokProvider, ProviderConfigError
from qa_agent.runtime import QAAgent, _BudgetLedger, BudgetExceeded
from qa_agent.store import Store
from qa_agent.util import load_json
from tests.b1_helpers import grok_cfg, agent_cfg, fake_key

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
    k = fake_key(); monkeypatch.setenv("XAI_API_KEY", k); return k


# ---------------- (5) budget readiness ----------------
@pytest.mark.cost
def test_repo_budget_defaults_are_proposed_eval_caps():
    b = load_json(ROOT / "config/agent.json")["budget"]
    assert (b["per_run_usd"], b["daily_usd"], b["total_usd"]) == (1.0, 15.0, 15.0) and b["scope"]
    g = load_json(ROOT / "config/agent.json")["providers"]["grok"]
    assert g["enabled"] is False and g["approved"] is False and all(v is None for v in g["pricing"].values() if not isinstance(v, str))


@pytest.mark.cost
@pytest.mark.parametrize("pricing", [
    {"input_per_mtok_usd": None, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": -1.0, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": 2.0, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 0},
    {"input_per_mtok_usd": "2", "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": True, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": 2.0, "output_per_mtok_usd": 6.0}])
def test_pricing_validation_fails_closed(key, pricing):
    with pytest.raises(ProviderConfigError, match="pricing"):
        GrokProvider(grok_cfg("http://127.0.0.1:9/v1", pricing=pricing))


@pytest.mark.cost
@pytest.mark.parametrize("bad", [{"total_usd": None}, {"total_usd": 0}, {"daily_usd": -1}, {"per_run_usd": "1"}, {"scope": ""}, {"scope": None}])
def test_billable_provider_requires_all_caps(tmp_path, key, bad):
    cfg = agent_cfg("grok", "http://127.0.0.1:9/v1"); cfg["budget"].update(bad)
    with pytest.raises(ProviderConfigError, match="budget"):
        QAAgent(tmp_path, agent_cfg=cfg)


def stub_reserve(st, run, est, day=None, scope="s1", total=15.0, per_run=1.0, daily=15.0):
    uid, reason = st.reserve_spend(run, "grok", "m", "plan_story", 0, 0, est, per_run, daily, total, scope)
    if uid and day:
        st.x("UPDATE usage SET day=? WHERE id=?", (day, uid))
    return uid, reason


@pytest.mark.cost
def test_total_cap_persists_across_runs_days_and_restarts(tmp_path):
    st = Store(tmp_path)
    for i in range(14):                                   # 14 runs x $1 on 14 different days (daily cap never hit)
        assert stub_reserve(st, f"r{i}", 1.0, day=f"2026-09-{i + 1:02d}")[0]
    st2 = Store(tmp_path)                                 # "restart": new connection, same DB
    assert stub_reserve(st2, "r14", 1.0, day="2026-09-20")[0]
    uid, reason = stub_reserve(st2, "r15", 0.5)
    assert uid is None and "total cap $15.0 for scope 's1'" in reason
    assert stub_reserve(st2, "r16", 0.5, scope="other-scope")[0]   # other scopes unaffected
    assert st2.scope_spend("s1") == pytest.approx(15.0)


@pytest.mark.cost
def test_total_cap_atomic_under_concurrency(tmp_path):
    Store(tmp_path)
    stores, ok, start = [Store(tmp_path) for _ in range(20)], [], threading.Barrier(20)
    def w(i):
        start.wait()
        if stub_reserve(stores[i], f"r{i}", 1.0, day=f"2026-08-{i + 1:02d}", per_run=5, daily=100)[0]:
            ok.append(i)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(20)]; [t.start() for t in ts]; [t.join() for t in ts]
    assert len(ok) == 15 and Store(tmp_path).scope_spend("s1") == pytest.approx(15.0)


@pytest.mark.cost
def test_total_cap_enforced_through_provider_ledger(tmp_path, key):
    cfg = agent_cfg("grok", "http://127.0.0.1:9/v1"); cfg["budget"].update(total_usd=1e-9, scope="tiny")
    a = QAAgent(tmp_path, agent_cfg=cfg)
    calls = []
    p = GrokProvider(grok_cfg("http://127.0.0.1:9/v1"), gateway=a.gateway, transport=httpx.MockTransport(lambda r: calls.append(r)), sleep=lambda x: None)
    with pytest.raises(BudgetExceeded, match="total cap"):
        p.generate("plan_story", CTX, {}, run_id="r", budget=_BudgetLedger(a, "r", "grok", "m", "plan_story", 0))
    assert calls == []
