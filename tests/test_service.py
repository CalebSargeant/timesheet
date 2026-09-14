"""The HTTP surface, end to end, with no network behind it.

The thing being proved here is the one that matters in a multi-user deployment:
no route hands over a week, a download or a setting without resolving the signed-in
account first, and none of them can be pointed at somebody else's."""
import importlib
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "week_2026-07-20.json").read_text(encoding="utf-8"))
KEY = "a-test-secret-key-long-enough-to-be-accepted"
ALICE = "github.com:1"
BOB = "github.com:2"


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A freshly imported service with a temp store and no live collectors."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SECRET_KEY", KEY)
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    monkeypatch.setenv("ACCESS_POLICY", "open")
    monkeypatch.setenv("PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "secret")

    from timesheet.service import main
    importlib.reload(main)
    monkeypatch.setattr(main, "_today", lambda _cfg: date(2026, 7, 22))
    monkeypatch.setattr(main, "_current_monday", lambda _cfg: date(2026, 7, 20))
    # No live pulls: the week comes from the captured fixture.
    monkeypatch.setattr(main, "collect_week",
                        lambda monday, cfg, sources=None, llm=None, full=False:
                        main.__dict__["_fixture_days"](cfg))

    from timesheet.collectors import normalize_activity, normalize_commits, normalize_meetings
    from timesheet.reconstruct import reconstruct_week
    from zoneinfo import ZoneInfo
    from datetime import datetime

    def _fixture_days(cfg):
        tz = ZoneInfo(cfg.tz)
        ws = datetime(2026, 7, 20, tzinfo=tz)
        return reconstruct_week(
            normalize_meetings(FIXTURE["meetings"], cfg, tz),
            normalize_commits(FIXTURE["commits"], tz), ws, cfg,
            events=normalize_activity(FIXTURE["activity"], tz),
            now=datetime(2026, 7, 22, 18, 0, tzinfo=tz))

    main._fixture_days = _fixture_days
    return main


@pytest.fixture
def client(app):
    return TestClient(app.app)


def sign_in(app, client, uid=ALICE, login="alice", **settings):
    from timesheet.service import auth
    from timesheet.service.users import User, defaults
    s = defaults()
    s.update({"tz": "Europe/Amsterdam", **settings})
    app._store.save_user(User(id=uid, login=login, name=login.title(), settings=s))
    client.cookies.set(auth.COOKIE, auth.make_session(uid))
    return app._store.get_user(uid)


# --- the door --------------------------------------------------------------


def test_a_stranger_sees_the_landing_page_not_a_timesheet(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Sign in with GitHub" in page.text
    assert "TOTAL" not in page.text


def test_every_data_route_refuses_a_stranger(client):
    for path in ("/timesheet.xlsx", "/settings", "/connections", "/status"):
        assert client.get(path).status_code == 401, path


def test_a_forged_session_is_not_a_session(client):
    from timesheet.service import auth
    client.cookies.set(auth.COOKIE, "made.up")
    assert client.get("/settings").status_code == 401


def test_a_session_for_a_deleted_account_is_not_a_session(app, client):
    from timesheet.service import auth
    client.cookies.set(auth.COOKIE, auth.make_session("github.com:999"))
    assert client.get("/settings").status_code == 401


def test_the_landing_page_says_so_when_oauth_is_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", KEY)
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_SECRET", raising=False)
    from timesheet.service import main
    importlib.reload(main)
    page = TestClient(main.app).get("/")
    assert "no GitHub OAuth app configured" in page.text


def test_login_redirects_to_the_provider(client):
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].startswith("https://github.com/login/oauth/authorize?")


def test_a_callback_with_an_unsigned_state_is_refused(client):
    r = client.get("/auth/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 400
    assert "expired or was not issued here" in r.text


# --- the timesheet ---------------------------------------------------------


def test_a_signed_in_account_sees_its_week(app, client):
    sign_in(app, client)
    page = client.get("/")
    assert page.status_code == 200
    assert "TOTAL" in page.text and "standup" in page.text.lower()
    assert "?period=last-week" in page.text
    assert 'type="date"' in page.text and 'name="from"' in page.text


def test_the_page_is_rendered_in_the_accounts_own_language(app, client):
    sign_in(app, client, locale="nl")
    assert "TOTAAL" in client.get("/").text
    assert "Deze week" in client.get("/").text


def test_a_custom_range_still_renders(app, client):
    sign_in(app, client)
    r = client.get("/?from=2026-07-20&to=2026-07-24")
    assert r.status_code == 200 and "TOTAL" in r.text


def test_the_download_is_a_real_workbook(app, client):
    sign_in(app, client)
    r = client.get("/timesheet.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


# --- signed links ----------------------------------------------------------


def test_a_signed_link_opens_without_a_session(app, client):
    """A manager clicks a link in an email, having never signed in."""
    sign_in(app, client)
    from timesheet.service import security
    token = security.sign_download(ALICE, "timesheet.xlsx", 600)
    client.cookies.clear()
    r = client.get(f"/d/{ALICE}/{token}/timesheet.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_a_signed_link_cannot_be_pointed_at_another_account(app, client):
    """Without the account id inside the signature, one valid link plus a guessed
    id reads anybody's timesheet."""
    sign_in(app, client)
    sign_in(app, client, uid=BOB, login="bob")
    from timesheet.service import security
    token = security.sign_download(ALICE, "timesheet.xlsx", 600)
    client.cookies.clear()
    assert client.get(f"/d/{BOB}/{token}/timesheet.xlsx").status_code == 403


def test_an_expired_signed_link_is_refused(app, client):
    sign_in(app, client)
    from timesheet.service import security
    token = security.sign_download(ALICE, "timesheet.xlsx", -1)
    client.cookies.clear()
    assert client.get(f"/d/{ALICE}/{token}/timesheet.xlsx").status_code == 403


# --- settings --------------------------------------------------------------


def test_settings_save_and_come_back(app, client):
    user = sign_in(app, client)
    from timesheet.service import auth
    r = client.post("/settings", data={
        "csrf": auth.csrf_token(user.id), "locale": "nl", "tz": "Europe/Amsterdam",
        "day_start": "09:00", "manager_email": "boss@example.invalid",
        "delivery_channel": "email", "delivery_enabled": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = app._store.get_user(ALICE)
    assert saved.settings["day_start"] == "09:00"
    assert saved.settings["manager_email"] == "boss@example.invalid"
    assert saved.settings["delivery_enabled"] is True


def test_an_unticked_checkbox_turns_the_setting_off(app, client):
    """A form post simply omits an unticked box. Read as 'missing, leave alone',
    nobody could ever switch delivery back off."""
    user = sign_in(app, client, delivery_enabled=True)
    from timesheet.service import auth
    client.post("/settings", data={"csrf": auth.csrf_token(user.id)},
                follow_redirects=False)
    assert app._store.get_user(ALICE).settings["delivery_enabled"] is False


def test_a_bad_value_is_reported_and_the_rest_is_kept(app, client):
    user = sign_in(app, client)
    from timesheet.service import auth
    r = client.post("/settings", data={
        "csrf": auth.csrf_token(user.id), "tz": "Nowhere/Nothing", "day_start": "09:15",
    })
    assert r.status_code == 400 and "not a known time zone" in r.text
    assert app._store.get_user(ALICE).settings["day_start"] == "09:15"


def test_a_post_without_a_csrf_token_is_refused(app, client):
    sign_in(app, client)
    assert client.post("/settings", data={"day_start": "09:00"}).status_code == 403


def test_another_accounts_csrf_token_is_refused(app, client):
    sign_in(app, client, uid=BOB, login="bob")
    sign_in(app, client)
    from timesheet.service import auth
    r = client.post("/settings", data={"csrf": auth.csrf_token(BOB), "day_start": "09:00"})
    assert r.status_code == 403


# --- connections -----------------------------------------------------------


def test_connections_reports_what_is_linked(app, client):
    sign_in(app, client)
    page = client.get("/connections")
    assert page.status_code == 200
    assert "Connected as alice" in page.text
    assert "Not connected" in page.text            # Microsoft


def test_connecting_microsoft_shows_a_code(app, client, monkeypatch):
    user = sign_in(app, client)
    from timesheet.collectors import mcp_client
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {
        "user_code": "ABC-123", "verification_uri": "https://microsoft.example.invalid",
        "device_code": "the-secret", "expires_in": 900, "interval": 5})
    from timesheet.service import auth
    r = client.post("/connect/microsoft", data={"csrf": auth.csrf_token(user.id)})
    assert r.status_code == 200
    assert "ABC-123" in r.text
    assert "the-secret" not in r.text              # the bearer secret never reaches a page


def test_polling_says_not_yet_while_the_human_has_not_finished(app, client, monkeypatch):
    user = sign_in(app, client)
    from timesheet.collectors import mcp_client
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {
        "user_code": "A", "verification_uri": "https://x.invalid",
        "device_code": "d", "expires_in": 900, "interval": 5})
    from timesheet.service import auth
    client.post("/connect/microsoft", data={"csrf": auth.csrf_token(user.id)})
    monkeypatch.setattr(mcp_client, "_post_form",
                        lambda *_a: {"error": "authorization_pending"})
    assert client.get("/connect/microsoft/poll").json() == {"done": False}


def test_a_completed_sign_in_stores_the_token_encrypted(app, client, monkeypatch):
    user = sign_in(app, client)
    from timesheet.collectors import mcp_client
    from timesheet.collectors.mcp_client import McpSession
    from timesheet.service.store import MICROSOFT
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {
        "user_code": "A", "verification_uri": "https://x.invalid",
        "device_code": "d", "expires_in": 900, "interval": 5})
    from timesheet.service import auth
    client.post("/connect/microsoft", data={"csrf": auth.csrf_token(user.id)})

    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {
        "access_token": "at", "refresh_token": "the-refresh-token"})
    monkeypatch.setattr(McpSession, "me", lambda _s: {"userPrincipalName": "alice@x.invalid"})
    assert client.get("/connect/microsoft/poll").json() == {"done": True}

    assert app._store.get_credential(ALICE, MICROSOFT)["refresh_token"] == "the-refresh-token"
    assert app._store.credential_meta(ALICE, MICROSOFT)["account"] == "alice@x.invalid"
    assert "Connected as alice@x.invalid" in client.get("/connections").text


def test_disconnecting_removes_the_token(app, client):
    user = sign_in(app, client)
    from timesheet.service.store import MICROSOFT
    from timesheet.service import auth
    app._store.put_credential(ALICE, MICROSOFT, {"refresh_token": "r"}, account="a@x.invalid")
    client.post("/connect/microsoft/disconnect", data={"csrf": auth.csrf_token(user.id)},
                follow_redirects=False)
    assert app._store.credential_meta(ALICE, MICROSOFT) is None


# --- ingest ----------------------------------------------------------------


def _ingest_payload():
    return {"week_start": "2026-07-20", "meetings": FIXTURE["meetings"]}


def test_ingest_needs_the_right_account_and_token(app, client, monkeypatch):
    sign_in(app, client)
    client.cookies.clear()
    from timesheet.service import security
    monkeypatch.setattr(app, "build_from_ingest",
                        lambda payload, cfg, sources=None, llm=None:
                        (date(2026, 7, 20), app._fixture_days(cfg)))

    assert client.post("/api/ingest", json=_ingest_payload()).status_code == 401
    assert client.post("/api/ingest", json=_ingest_payload(),
                       headers={"X-Account": ALICE, "X-Ingest-Token": "wrong"}
                       ).status_code == 401
    ok = client.post("/api/ingest", json=_ingest_payload(), headers={
        "X-Account": ALICE, "X-Ingest-Token": security.ingest_token(ALICE)})
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_one_accounts_ingest_token_does_not_open_another(app, client):
    """A deployment-wide secret would let any leaked automation write into
    everybody's timesheet."""
    sign_in(app, client)
    sign_in(app, client, uid=BOB, login="bob")
    client.cookies.clear()
    from timesheet.service import security
    r = client.post("/api/ingest", json=_ingest_payload(), headers={
        "X-Account": BOB, "X-Ingest-Token": security.ingest_token(ALICE)})
    assert r.status_code == 401


# --- delivery --------------------------------------------------------------


def test_send_now_reports_what_actually_happened(app, client, monkeypatch):
    user = sign_in(app, client, delivery_channel="email", delivery_enabled=True,
                   manager_email="boss@example.invalid")
    from timesheet.service import auth, delivery
    monkeypatch.setattr(delivery, "send", lambda *a, **k: delivery.Delivery(
        "email", False, target="boss@example.invalid", reason="the mail server said no"))
    r = client.post("/deliver", data={"csrf": auth.csrf_token(user.id)},
                    follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert "mail+server+said+no" in r.headers["location"].replace("%20", "+")


# --- housekeeping ----------------------------------------------------------


def test_healthz_stays_database_free(client):
    """A database call here turns a slow database into a crash-loop, which is
    precisely when the pod needs to stay up."""
    assert client.get("/healthz").json() == {"ok": True}


def test_signing_out_clears_the_cookie(app, client):
    user = sign_in(app, client)
    from timesheet.service import auth
    r = client.post("/auth/logout", data={"csrf": auth.csrf_token(user.id)},
                    follow_redirects=False)
    assert "Max-Age=0" in r.headers["set-cookie"]


def test_deleting_an_account_needs_the_word(app, client):
    user = sign_in(app, client)
    from timesheet.service import auth
    client.post("/account/delete", data={"csrf": auth.csrf_token(user.id), "confirm": "yes"},
                follow_redirects=False)
    assert app._store.get_user(ALICE) is not None
    client.post("/account/delete", data={"csrf": auth.csrf_token(user.id), "confirm": "DELETE"},
                follow_redirects=False)
    assert app._store.get_user(ALICE) is None
