import contextlib, copy, os, secrets, socket, subprocess, sys, time
import httpx
from qa_agent import ROOT
from qa_agent.util import load_json

# FAKE test prices (USD per 1M tokens) - only for exercising the cost arithmetic. Not provider's prices.
TEST_PRICING = {"input_per_mtok_usd": 2.0, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def compatible_cfg(base_url, **over):
    c = copy.deepcopy(load_json(ROOT / "config/agent.json")["providers"]["compatible"])
    c.update(enabled=True, base_url=base_url, pricing=dict(TEST_PRICING), backoff_base_s=0.01, max_backoff_s=0.05, timeout_s=5)
    c.update(over)
    return c


def agent_cfg(provider="compatible", base_url=None, **section_over):
    c = copy.deepcopy(load_json(ROOT / "config/agent.json"))
    c["model"]["provider"] = provider
    if base_url:
        c["providers"]["compatible"] = compatible_cfg(base_url)
    for k, v in section_over.items():
        c[k] = {**c.get(k, {}), **v} if isinstance(v, dict) else v
    return c


def fake_key():
    return "sk-TESTSECRET" + secrets.token_hex(12)


@contextlib.contextmanager
def model_stub():
    port = free_port()
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "qa_agent.devtools.model_stub:app", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/_stub/requests").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        yield StubClient(url)
    finally:
        p.terminate(); p.wait(10)


class StubClient:
    def __init__(self, url):
        self.url, self.base = url, url + "/v1"

    def queue(self, items):
        httpx.post(self.url + "/_stub/queue", json=items)

    def requests(self):
        return httpx.get(self.url + "/_stub/requests").json()

    def reset(self):
        httpx.post(self.url + "/_stub/reset")


def wait_status(get, statuses, timeout=120):
    end = time.time() + timeout
    r = None
    while time.time() < end:
        r = get()
        if r["status"] in statuses:
            return r
        time.sleep(0.2)
    raise AssertionError(f"timeout; last {r and r['status']}: {r and r.get('error')}")
