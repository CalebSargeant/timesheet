"""Sign-in: signed state, signed sessions, and the policy deciding who gets in.

These are the controls standing between a stranger and a database of delegated
mailbox tokens, so every one of them is pinned down here."""
import time

import pytest

from timesheet.service import auth
from timesheet.service.store import FileStore
from timesheet.service.users import User, defaults

KEY = "a-test-secret-key-long-enough-to-be-accepted"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", KEY)


def _store(tmp_path) -> FileStore:
    return FileStore(str(tmp_path))


def _profile(account_id=1, login="alice"):
    return {"id": account_id, "login": login, "name": "Alice", "avatar_url": ""}


# --- signing ---------------------------------------------------------------


def test_a_session_round_trips():
    assert auth.read_session(auth.make_session("github.com:1")) == "github.com:1"


def test_a_tampered_session_is_rejected():
    mac = auth.make_session("github.com:1").split(".", 1)[1]
    forged = auth._b64(b'{"uid":"github.com:2","exp":9999999999}') + "." + mac
    assert auth.read_session(forged) is None


def test_a_session_signed_with_another_key_is_rejected(monkeypatch):
    token = auth.make_session("github.com:1")
    monkeypatch.setenv("SECRET_KEY", "a-completely-different-secret-key-here")
    assert auth.read_session(token) is None


def test_an_expired_session_is_rejected(monkeypatch):
    monkeypatch.setattr(auth, "SESSION_TTL", -1)
    assert auth.read_session(auth.make_session("github.com:1")) is None


def test_no_secret_key_means_nobody_signs_in(monkeypatch):
    """A default key is a public key. Refusing outright beats a deployment that
    looks fine and is signing sessions anyone can forge."""
    monkeypatch.setenv("SECRET_KEY", "")
    with pytest.raises(auth.AuthError, match="SECRET_KEY"):
        auth.make_session("github.com:1")


def test_state_carries_only_a_local_path():
    """Signing an absolute URL would turn the callback into an open redirect."""
    assert auth.read_state(auth.make_state("/settings")) == "/settings"
    assert auth.read_state(auth.make_state("https://evil.example/steal")) == "/"
    assert auth.read_state(auth.make_state("//evil.example/steal")) == "/"


def test_an_unsigned_state_is_refused():
    assert auth.read_state("not-a-real-state") is None


def test_expired_state_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "STATE_TTL", -1)
    assert auth.read_state(auth.make_state("/")) is None


def test_csrf_is_bound_to_one_session():
    token = auth.csrf_token("github.com:1")
    assert auth.check_csrf("github.com:1", token)
    assert not auth.check_csrf("github.com:2", token)
    assert not auth.check_csrf("github.com:1", None)


def test_a_session_cookie_is_not_a_csrf_token():
    """They are signed with the same key, so the payload has to tell them apart."""
    assert not auth.check_csrf("github.com:1", auth.make_session("github.com:1"))


# --- provider hosts --------------------------------------------------------


def test_dotcom_and_enterprise_resolve_to_different_api_bases():
    """github.com serves its API from a separate hostname; Enterprise Server
    serves it from /api/v3 on the same one. Getting this wrong 404s every call."""
    assert auth.Provider(host="github.com").api == "https://api.github.com"
    assert auth.Provider(host="ghe.example.invalid").api == "https://ghe.example.invalid/api/v3"
    assert auth.Provider(host="https://ghe.example.invalid/").web == "https://ghe.example.invalid"


def test_the_authorize_url_is_built_on_the_right_host():
    p = auth.Provider(client_id="cid", client_secret="s", host="ghe.example.invalid",
                      base_url="https://timesheet.example.invalid")
    url = p.authorize_url("STATE")
    assert url.startswith("https://ghe.example.invalid/login/oauth/authorize?")
    assert "state=STATE" in url
    assert "timesheet.example.invalid%2Fauth%2Fcallback" in url


# --- policy ----------------------------------------------------------------


def test_open_admits_anybody():
    auth.check_access(auth.Policy("open"), "stranger", "x", existing=False,
                      user_count=99, orgs=[])


def test_single_admits_the_first_and_then_nobody_else():
    """A fresh deployment of something that stores delegated mailbox tokens
    should not be an open door."""
    policy = auth.Policy("single")
    auth.check_access(policy, "alice", "x", existing=False, user_count=0, orgs=[])
    auth.check_access(policy, "alice", "x", existing=True, user_count=1, orgs=[])
    with pytest.raises(auth.NotAllowed, match="single account"):
        auth.check_access(policy, "mallory", "y", existing=False, user_count=1, orgs=[])


def test_single_is_the_default():
    assert auth.Policy.from_env({}).mode == "single"


def test_allowlist_is_case_insensitive():
    policy = auth.Policy("allowlist", logins=frozenset({"alice"}))
    auth.check_access(policy, "ALICE", "x", existing=False, user_count=0, orgs=[])
    with pytest.raises(auth.NotAllowed):
        auth.check_access(policy, "mallory", "y", existing=False, user_count=0, orgs=[])


def test_org_policy_checks_membership():
    policy = auth.Policy("org", orgs=("acme",))
    auth.check_access(policy, "alice", "x", existing=False, user_count=0, orgs=["Acme"])
    with pytest.raises(auth.NotAllowed, match="public or the"):
        auth.check_access(policy, "mallory", "y", existing=False, user_count=0, orgs=["other"])


def test_org_policy_with_no_orgs_admits_nobody():
    """Failing open here would turn a misconfiguration into an open door."""
    with pytest.raises(auth.NotAllowed):
        auth.check_access(auth.Policy("org"), "alice", "x", existing=False,
                          user_count=0, orgs=["acme"])


def test_a_misspelt_policy_is_refused_at_startup():
    with pytest.raises(ValueError, match="ACCESS_POLICY"):
        auth.Policy.from_env({"ACCESS_POLICY": "everyone"})


# --- account creation ------------------------------------------------------


def test_an_account_is_keyed_on_the_numeric_id_not_the_login(tmp_path):
    """A GitHub login can be renamed and, once released, registered by a
    stranger. Keying on it would hand them the previous holder's tokens."""
    store = _store(tmp_path)
    provider = auth.Provider(host="github.com")
    policy = auth.Policy("open")
    first = auth.upsert(store, provider, policy, _profile(7, "alice"), "a@x.invalid", [])
    renamed = auth.upsert(store, provider, policy, _profile(7, "alice-new"), "a@x.invalid", [])
    impostor = auth.upsert(store, provider, policy, _profile(8, "alice"), "m@x.invalid", [])
    assert first.id == renamed.id == "github.com:7"
    assert impostor.id == "github.com:8"
    assert store.count_users() == 2


def test_the_first_account_administers_the_deployment(tmp_path):
    store = _store(tmp_path)
    provider, policy = auth.Provider(host="github.com"), auth.Policy("open")
    first = auth.upsert(store, provider, policy, _profile(1, "alice"), "", [])
    second = auth.upsert(store, provider, policy, _profile(2, "bob"), "", [])
    assert first.is_admin and not second.is_admin


def test_signing_in_seeds_the_github_identity(tmp_path):
    """So a new account can pull its own commits before touching a setting."""
    store = _store(tmp_path)
    user = auth.upsert(store, auth.Provider(host="ghe.example.invalid"), auth.Policy("open"),
                       _profile(1, "alice"), "", [])
    assert user.settings["github_user"] == "alice"
    assert user.settings["github_host"] == "ghe.example.invalid"


def test_existing_settings_survive_a_later_sign_in(tmp_path):
    store = _store(tmp_path)
    provider, policy = auth.Provider(host="github.com"), auth.Policy("open")
    user = auth.upsert(store, provider, policy, _profile(1, "alice"), "", [])
    user.settings["manager_email"] = "boss@example.invalid"
    user.settings["locale"] = "nl"
    store.save_user(user)
    again = auth.upsert(store, provider, policy, _profile(1, "alice"), "", [])
    assert again.settings["manager_email"] == "boss@example.invalid"
    assert again.settings["locale"] == "nl"


def test_a_profile_without_an_id_is_refused(tmp_path):
    with pytest.raises(auth.AuthError, match="account id"):
        auth.upsert(_store(tmp_path), auth.Provider(), auth.Policy("open"),
                    {"login": "alice"}, "", [])


def test_a_rejected_account_is_not_created(tmp_path):
    store = _store(tmp_path)
    store.save_user(User(id="github.com:1", login="alice", settings=defaults()))
    with pytest.raises(auth.NotAllowed):
        auth.upsert(store, auth.Provider(), auth.Policy("single"),
                    _profile(2, "mallory"), "", [])
    assert store.get_user("github.com:2") is None


# --- cookies ---------------------------------------------------------------


def test_the_cookie_is_hardened():
    header = auth.cookie_header("v", secure=True)
    assert "HttpOnly" in header and "SameSite=Lax" in header and "Secure" in header


def test_a_plain_http_deployment_does_not_claim_secure():
    """Marking it Secure over http means the browser never sends it back, and
    the symptom is a sign-in loop with no error anywhere."""
    assert "Secure" not in auth.cookie_header("v", secure=False)


def test_clearing_the_cookie_expires_it_immediately():
    assert "Max-Age=0" in auth.clear_cookie(secure=True)


def test_state_and_session_tokens_are_not_interchangeable():
    state = auth.make_state("/")
    assert auth.read_session(state) is None      # no uid in a state payload
    assert time.time() > 0                        # sanity: the clock is real
