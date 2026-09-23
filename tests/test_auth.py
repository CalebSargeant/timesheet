"""Sign-in: signed state, signed sessions, and the policy deciding who gets in.

These are the controls standing between a stranger and a database of delegated
mailbox tokens, so every one of them is pinned down here."""
import time

import pytest

from timesheet.service import auth
from timesheet.service.store import GITHUB, FileStore
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


@pytest.mark.parametrize("sneaky", [
    "/\\evil.example/steal",       # a browser reads the backslash as a slash
    "/\\/evil.example",
    "/ok\r\nLocation: https://evil.example",
    "\t/evil.example",
])
def test_state_refuses_the_paths_that_only_look_local(sneaky):
    """A leading-`//` check alone let `/\\evil.example` through, and
    `/auth/login?next=` made it a sign-in link that ended on another site."""
    assert auth.read_state(auth.make_state(sneaky)) == "/"


def test_the_redirect_sink_itself_refuses_to_leave_the_host():
    """Checked again where every redirect is issued, so a future caller that
    forgets cannot reopen it."""
    from timesheet.service import main
    for target in ("https://evil.example/", "//evil.example", "/\\evil.example"):
        assert main._redirect(target).headers["location"] == "/"
    assert main._redirect("/connections?error=x").headers["location"] == "/connections?error=x"


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


# --- the GitHub token --------------------------------------------------------
#
# An OAuth app registered since GitHub's August 2026 change gets an access token
# that lives eight hours and a refresh token that lives six months. Keeping only
# the access token cost every account its GitHub activity eight hours after it
# signed in, and nothing said so.

UID = "github.com:1"
PROVIDER = auth.Provider(client_id="cid", client_secret="csecret", host="github.com")
EXPIRING = {"access_token": "ghu_new", "token_type": "bearer", "scope": "repo",
            "expires_in": 28800, "refresh_token": "ghr_new",
            "refresh_token_expires_in": 15811200}


def _with_token(tmp_path, **cred) -> FileStore:
    store = _store(tmp_path)
    store.put_credential(UID, GITHUB, cred, account="alice")
    return store


def _answer(got: dict, calls: list | None = None):
    def _post(url, data, *, timeout=20):
        if calls is not None:
            calls.append((url, data))
        return got
    return _post


def test_an_expiring_sign_in_keeps_its_refresh_token_and_expiry(monkeypatch):
    monkeypatch.setattr(auth, "_post_json", _answer(EXPIRING))
    before = time.time()
    tokens = auth.exchange(PROVIDER, "code")
    assert tokens["access_token"] == "ghu_new"
    assert tokens["refresh_token"] == "ghr_new"
    assert int(before) + 28800 <= tokens["expires_at"] <= time.time() + 28800
    assert tokens["refresh_expires_at"] > tokens["expires_at"]


def test_a_token_that_never_expires_is_stored_as_it_always_was(monkeypatch):
    monkeypatch.setattr(auth, "_post_json",
                        _answer({"access_token": "gho_x", "token_type": "bearer"}))
    assert auth.exchange(PROVIDER, "code") == {"access_token": "gho_x"}


def test_a_live_token_is_used_as_it_is(tmp_path, monkeypatch):
    store = _with_token(tmp_path, access_token="ghu_old", refresh_token="ghr_old",
                        expires_at=time.time() + 3600)
    calls: list = []
    monkeypatch.setattr(auth, "_post_json", _answer(EXPIRING, calls))
    assert auth.github_token(store, PROVIDER, UID) == "ghu_old"
    assert calls == []


def test_an_expiring_token_is_renewed_and_the_new_pair_stored(tmp_path, monkeypatch):
    store = _with_token(tmp_path, access_token="ghu_old", refresh_token="ghr_old",
                        expires_at=time.time() + 60)     # inside the renewal margin
    calls: list = []
    monkeypatch.setattr(auth, "_post_json", _answer(EXPIRING, calls))
    assert auth.github_token(store, PROVIDER, UID) == "ghu_new"
    url, data = calls[0]
    assert url == "https://github.com/login/oauth/access_token"
    assert data == {"client_id": "cid", "client_secret": "csecret",
                    "grant_type": "refresh_token", "refresh_token": "ghr_old"}
    # The old refresh token is spent the moment it is used, so the new one must
    # be stored or the next renewal has nothing to renew with.
    assert store.get_credential(UID, GITHUB)["refresh_token"] == "ghr_new"
    assert store.credential_meta(UID, GITHUB)["account"] == "alice"


def test_losing_the_renewal_race_uses_the_winners_token(tmp_path, monkeypatch):
    """The web process and the refresh job can find the same expired token at
    once. The second to renew is refused, but the first has stored a new pair."""
    store = _with_token(tmp_path, access_token="ghu_old", refresh_token="ghr_old",
                        expires_at=time.time() - 1)

    def _beaten_to_it(url, data, *, timeout=20):
        store.put_credential(UID, GITHUB, {"access_token": "ghu_winner",
                                           "refresh_token": "ghr_winner",
                                           "expires_at": time.time() + 28800})
        return {"error": "bad_refresh_token"}

    monkeypatch.setattr(auth, "_post_json", _beaten_to_it)
    assert auth.github_token(store, PROVIDER, UID) == "ghu_winner"
    assert store.get_credential(UID, GITHUB)["refresh_token"] == "ghr_winner"


def test_a_refused_renewal_hands_back_the_old_token(tmp_path, monkeypatch):
    """So GitHub's 401 on it tells the person to sign in again, instead of their
    GitHub activity vanishing without a word."""
    store = _with_token(tmp_path, access_token="ghu_old", refresh_token="ghr_old",
                        expires_at=time.time() - 1)
    monkeypatch.setattr(auth, "_post_json", _answer(
        {"error": "bad_refresh_token", "error_description": "The refresh token is expired."}))
    assert auth.github_token(store, PROVIDER, UID) == "ghu_old"
    assert store.get_credential(UID, GITHUB)["refresh_token"] == "ghr_old"


def test_a_token_stored_before_renewal_existed_is_passed_through(tmp_path, monkeypatch):
    """No refresh token to renew with. If GitHub still takes it, nothing changes."""
    store = _with_token(tmp_path, access_token="gho_legacy")
    calls: list = []
    monkeypatch.setattr(auth, "_post_json", _answer(EXPIRING, calls))
    assert auth.github_token(store, PROVIDER, UID) == "gho_legacy"
    assert calls == []


def test_a_rejected_token_is_named_as_such(monkeypatch):
    import urllib.error

    def _unauthorised(req, *, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(auth, "open_url", _unauthorised)
    assert auth.github_problem(PROVIDER, "ghu_dead") == "GitHub no longer accepts this sign-in"
    assert auth.github_problem(PROVIDER, "") == "no GitHub token is stored for this account"
    monkeypatch.setattr(auth, "_get_json", lambda url, token: {"login": "alice"})
    assert auth.github_problem(PROVIDER, "ghu_live") == ""
