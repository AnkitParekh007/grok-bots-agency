import json, time
from urllib.parse import parse_qs, urlsplit
import httpx, jwt, pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from qa_agent.store import Store
from qa_agent import oidc as draft

pytestmark = [pytest.mark.security, pytest.mark.approval]

@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def client(tmp_path, monkeypatch, signing_key):
    monkeypatch.setenv("TEST_OIDC_FLOW_KEY", Fernet.generate_key().decode())
    cfg = {"issuer": "https://idp.invalid", "authorization_endpoint": "https://idp.invalid/authorize",
           "token_endpoint": "https://idp.invalid/token", "jwks_uri": "https://idp.invalid/jwks",
           "redirect_uri": "https://workspace.invalid/api/oidc/callback", "client_id": "qa-workspace",
           "allowed_hosts": ["idp.invalid"], "approval_ref": "synthetic-test", "flow_key_env": "TEST_OIDC_FLOW_KEY",
           "required_amr": ["mfa"]}
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update(kid="test-key", alg="RS256", use="sig")
    returned, calls = {}, []
    def respond(req):
        calls.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [jwk]})
        assert req.method == "POST" and req.url.path == "/token"
        assert b"code_verifier=" in req.content
        return httpx.Response(200, json=returned)
    store = Store(tmp_path)
    one = draft.OidcClient(cfg, store, transport=httpx.MockTransport(respond))
    yield one, returned, calls, store
    one.close();store.db.close()


def claims(nonce_value, **over):
    return {"iss": "https://idp.invalid", "aud": "qa-workspace", "sub": "stable-subject", "iat": int(time.time()), "auth_time": int(time.time()),
            "exp": int(time.time()) + 600, "nonce": nonce_value, "amr": ["mfa"], **over}


def signed(signing_key, values, **headers):
    return jwt.encode(values, signing_key, algorithm="RS256", headers={"kid": "test-key", **headers})


def test_flow_pkce_nonce_and_single_use(client, signing_key):
    one, returned, calls, store = client
    flow = one.begin();query = parse_qs(urlsplit(flow["url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    returned["id_token"] = signed(signing_key, claims(query["nonce"][0]))
    assert one.complete("synthetic-code", query["state"][0], flow["binding"])["sub"] == "stable-subject"
    with pytest.raises(draft.OidcError, match="replayed"):
        one.complete("synthetic-code", query["state"][0], flow["binding"])
    assert calls == [("POST", "/token"), ("GET", "/jwks")]


def test_flow_is_browser_bound_and_verifier_encrypted(client):
    one, _, calls, store = client
    flow = one.begin();query = parse_qs(urlsplit(flow["url"]).query)
    row = store.q("SELECT * FROM oidc_flows")[0]
    assert flow["binding"] not in json.dumps(row)
    assert row["verifier"].startswith("gAAAA")
    with pytest.raises(draft.OidcError, match="unbound"):
        one.complete("synthetic-code", query["state"][0], "wrong-browser")
    assert calls == []


@pytest.mark.parametrize("over", [{"iss": "https://wrong.invalid"}, {"aud": "wrong-client"}, {"exp": 1},
                                  {"nonce": "wrong"}, {"amr": ["pwd"]}, {"azp": "wrong-client"}, {"auth_time": 1}])
def test_claim_failures_are_rejected(client, signing_key, over):
    one, _, _, _ = client
    with pytest.raises(draft.OidcError, match="validation failed"):
        one.verify(signed(signing_key, claims("expected", **over)), expected_nonce_hash=draft.token_hash("expected"))


@pytest.mark.parametrize("header", [{"jku": "https://attacker.invalid/jwks"}, {"kid": "unknown"}, {"crit": ["evil"]}])
def test_untrusted_token_key_headers_cannot_choose_network(client, signing_key, header):
    one, _, calls, _ = client
    with pytest.raises(draft.OidcError):
        one.verify(signed(signing_key, claims("expected"), **header), expected_nonce_hash=draft.token_hash("expected"))
    assert all(path == "/jwks" for _, path in calls)


def test_none_algorithm_is_rejected_without_fetch(client):
    one, _, calls, _ = client
    token = jwt.encode(claims("expected"), key=None, algorithm="none", headers={"kid": "test-key"})
    with pytest.raises(draft.OidcError):
        one.verify(token, expected_nonce_hash=draft.token_hash("expected"))
    assert calls == []


def test_backchannel_logout_event_replay(client, signing_key):
    one, _, _, _ = client
    values = {"iss": "https://idp.invalid", "aud": "qa-workspace", "iat": int(time.time()), "jti": "one-event",
              "sid": "idp-session", "events": {"http://schemas.openid.net/event/backchannel-logout": {}}}
    token = signed(signing_key, values)
    assert one.consume_logout(token)["sid"] == "idp-session"
    with pytest.raises(draft.OidcError, match="replayed"):
        one.consume_logout(token)
