import concurrent.futures
import pytest
from qa_agent.store import Store
from qa_agent.providers.quota import QuotaManager, QuotaExceeded, retry_seconds

pytestmark = [pytest.mark.provider, pytest.mark.security, pytest.mark.cost]


def test_quota_atomic_across_connections(tmp_path):
    managers = [QuotaManager(Store(tmp_path)) for _ in range(6)]
    def take(i):
        try:
            return managers[i].reserve("account", 10, {"rpm": 3, "tpm": 30, "concurrency": 8})
        except QuotaExceeded:
            return None
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        assert sum(x is not None for x in pool.map(take, range(6))) == 3


@pytest.mark.parametrize("limits", [{"rpm": 1}, {"rpd": 1}, {"tpm": 10}, {"tpd": 10}, {"concurrency": 1}])
def test_each_quota_dimension(tmp_path, limits):
    q = QuotaManager(Store(tmp_path), clock=lambda: 1000)
    q.reserve("a", 10, dict(concurrency=10, **limits) if "concurrency" not in limits else limits)
    with pytest.raises(QuotaExceeded):
        q.reserve("a", 1, dict(concurrency=10, **limits) if "concurrency" not in limits else limits)
    q.reserve("different-account", 1, limits)


def test_quota_reset_unknown_usage_and_crash_lease(tmp_path):
    t = [1000]
    q = QuotaManager(Store(tmp_path), clock=lambda: t[0])
    assert q.status("a", {})["remaining"] is None
    q.reserve("a", 10, {"rpm": 1, "tpm": 10}, lease_s=1)
    t[0] = 1007
    with pytest.raises(QuotaExceeded):
        q.reserve("a", 1, {"rpm": 1, "tpm": 10})
    t[0] = 1020
    q.reserve("a", 10, {"rpm": 1, "tpm": 10})
    assert q.status("a", {})["local_reservations"]["tokens"] == 20


def test_custom_reset_windows_and_refund_only_not_sent(tmp_path):
    t = [100]
    q = QuotaManager(Store(tmp_path), clock=lambda: t[0])
    limits = {"rpd": 1, "day_window_s": 200, "reset_offset_s": 50}
    r = q.reserve("a", 10, limits)
    q.settle(r, outcome="denied_not_sent")
    r = q.reserve("a", 10, limits)
    q.settle(r, outcome="timeout")
    t[0] = 249
    with pytest.raises(QuotaExceeded): q.reserve("a", 1, limits)
    t[0] = 250
    q.reserve("a", 1, limits)


def test_retry_after_persists_and_usage_reconciles(tmp_path):
    t = [100]
    q = QuotaManager(Store(tmp_path), clock=lambda: t[0])
    r = q.reserve("a", 100, {})
    q.settle(r, 20, "stop")
    q.observe("a", {"retry-after": "120", "x-ratelimit-remaining-tokens": "40"})
    assert q.status("a", {})["remaining"]["tokens"] == 40
    assert q.status("a", {})["local_reservations"]["tokens"] == 20
    with pytest.raises(QuotaExceeded, match="Retry-After"): q.reserve("a", 1, {})
    t[0] = 221
    q.reserve("a", 1, {})
    assert retry_seconds("Thu, 01 Jan 1970 00:05:00 GMT", lambda: 100) == 200
    assert retry_seconds("nan") is None
