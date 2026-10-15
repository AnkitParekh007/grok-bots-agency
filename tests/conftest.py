import os, socket, subprocess, sys, time, contextlib
import httpx, pytest
from qa_agent import ROOT


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def sut(defects: str):
    port = _free_port()
    env = dict(os.environ, SUT_DEFECTS=defects)
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "sut.app:app", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("SUT did not start")
        yield url
    finally:
        p.terminate()
        p.wait(10)


@pytest.fixture
def sut_factory():
    return sut


@pytest.fixture(autouse=True)
def offline_default(monkeypatch):
    monkeypatch.setenv("QA_MODEL_PROVIDER", "replay")


@pytest.fixture(autouse=True)
def deny_external_inference_network(monkeypatch):
    original = socket.socket.connect
    def guarded(self, address):
        if isinstance(address, tuple):
            import ipaddress
            try:
                local = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = address[0] == "localhost"
            if not local:
                raise AssertionError(f"offline test attempted external network: {address}")
        return original(self, address)
    monkeypatch.setattr(socket.socket, "connect", guarded)
