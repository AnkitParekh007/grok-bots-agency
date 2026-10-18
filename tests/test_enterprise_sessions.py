import json
import pytest
from cryptography.fernet import Fernet
from qa_agent.store import Store
from qa_agent.auth import UserStore
from qa_agent import sessions as draft

pytestmark = [pytest.mark.security, pytest.mark.approval]

@pytest.fixture
def sessions(tmp_path):
    clock = [1000.0]
    store = Store(tmp_path / "store");users = UserStore(tmp_path / "users.json")
    users.add("alice", "requester")
    one = draft.SessionManager(store, users, key=Fernet.generate_key(), shared=True, idle_s=60, lifetime_s=300,
                               max_per_identity=2, clock=lambda: clock[0])
    yield one, clock, store, users
    store.db.close()


P = {"name": "alice", "mode": "local", "role": "requester"}


def test_opaque_hashed_session_and_csrf_revocation(sessions):
    one, _, store, _ = sessions
    issued = one.issue(P)
    row = dict(store.q("SELECT * FROM web_sessions")[0])
    assert issued["token"] not in json.dumps(row) and issued["csrf"] not in json.dumps(row)
    assert row["payload"].startswith("gAAAA")
    assert one.verify(issued["token"]) == P
    assert not one.csrf_ok(issued["token"], issued["csrf"], "x" * 43)
    assert one.csrf_ok(issued["token"], issued["csrf"], issued["csrf"])
    one.revoke(issued["token"])
    assert one.verify(issued["token"]) is None
    assert not one.csrf_ok(issued["token"], issued["csrf"], issued["csrf"])


def test_idle_and_absolute_expiry(sessions):
    one, clock, _, _ = sessions
    issued = one.issue(P)
    clock[0] += 59
    assert one.verify(issued["token"])
    clock[0] += 61
    assert one.verify(issued["token"]) is None
    clock[0] = 2000
    other = one.issue(P)
    for t in range(2010, 2300, 10):
        clock[0] = t;assert one.verify(other["token"])
    clock[0] = 2300
    assert one.verify(other["token"]) is None


def test_rotation_and_disable_invalidate_existing_browser_session(sessions):
    one, _, _, users = sessions
    issued = one.issue(P)
    users.add("alice", "requester")
    assert one.verify(issued["token"]) is None
    other = one.issue(P)
    values = users._load();values["users"]["alice"]["disabled"] = True
    users.path.write_text(json.dumps(values))
    assert one.verify(other["token"]) is None


def test_session_admission_and_signed_expiry_tamper(sessions):
    one, _, store, _ = sessions
    issued = one.issue(P);one.issue(P)
    with pytest.raises(ValueError, match="limit"):
        one.issue(P)
    store.x("UPDATE web_sessions SET expires=expires+10000 WHERE token_hash=?", (draft._hash(issued["token"]),))
    assert one.verify(issued["token"]) is None


def test_external_logout_scopes_issuer_and_session(sessions):
    one, _, _, _ = sessions
    a = one.issue({"name": "issuer-a|subject", "mode": "oidc", "role": "viewer"}, issuer="issuer-a", subject="subject", sid="same-sid")
    b = one.issue({"name": "issuer-b|subject", "mode": "oidc", "role": "viewer"}, issuer="issuer-b", subject="subject", sid="same-sid")
    one.revoke_external("issuer-a", sid="same-sid")
    assert one.verify(a["token"]) is None
    assert one.verify(b["token"])


def test_shared_profile_cannot_use_unencrypted_sessions(tmp_path):
    store = Store(tmp_path)
    try:
        with pytest.raises(ValueError, match="encryption"):
            draft.SessionManager(store, shared=True)
    finally:
        store.db.close()


def test_encrypted_principal_cannot_be_swapped_between_session_tokens(sessions):
    one, _, store, _ = sessions
    first = one.issue({"name": "one", "mode": "oidc", "role": "viewer"})
    other = one.issue({"name": "two", "mode": "oidc", "role": "approver"})
    row = store.q("SELECT * FROM web_sessions WHERE token_hash=?", (draft._hash(other["token"]),))[0]
    store.x("UPDATE web_sessions SET payload=?,expires=?,identity=? WHERE token_hash=?",
            (row["payload"], row["expires"], row["identity"], draft._hash(first["token"])))
    assert one.verify(first["token"]) is None


def test_rotating_a_scoped_service_key_cannot_broaden_its_identity(tmp_path):
    from datetime import datetime, timedelta, timezone
    users = UserStore(tmp_path / "users.json")
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    old = users.add("service", "requester", tenant_id="tenant-a", project_roles={"PCC": "requester"},
                    kind="service", expires_at=expires)
    new = users.add("service", "requester")
    assert users.verify(old) is None
    principal = users.verify(new)
    assert principal.kind == "service" and principal.tenant_id == "tenant-a" and principal.project_roles == {"PCC": "requester"}
    assert users._load()["users"]["service"]["expires_at"] == expires


def test_background_reference_checks_do_not_extend_idle_session(sessions):
    one, clock, _, _ = sessions
    issued = one.issue(P)
    reference = draft._hash(issued["token"])
    clock[0] += 50
    assert one.verify_reference(reference) == P
    clock[0] += 11
    assert one.verify_reference(reference) is None
