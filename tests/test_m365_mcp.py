"""The MCP collector and transport, against captured connector payloads. No network.

Shapes here came from live `outlook_calendar_search` / `outlook_email_search` /
`chat_message_search` responses, stripped of anything identifying. They exist so
a change in the connector's output is caught here rather than by a sheet that
quietly reconstructs a week of leave as eight-hour working days.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import pathlib
import time
import urllib.error
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timesheet.collectors import m365_mcp, mcp_client, normalize_full_days, normalize_meetings
from timesheet.collectors.mcp_client import DeviceCode, McpSession
from timesheet.config import Config
from timesheet.pipeline import Sources, collect_week

UTC = ZoneInfo("UTC")
AMS = ZoneInfo("Europe/Amsterdam")
CFG = Config(tz="Europe/Amsterdam")


def meeting(day="2026-09-01", start="06:45:00", end="07:30:00", **over):
    raw = {
        "uri": f"calendar:///events/{day}{start}",
        "id": f"{day}{start}",
        "subject": "!! Daily standup !!",
        "organizer": "a.colleague@example.invalid",
        "start": {"dateTime": f"{day}T{start}.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{day}T{end}.0000000", "timeZone": "UTC"},
        "showAs": "busy",
        "isAllDay": False,
        "isCancelled": False,
    }
    raw.update(over)
    return raw


def all_day(first="2026-09-03", after="2026-09-04", **over):
    raw = {
        "uri": f"calendar:///events/{first}-allday",
        "id": f"{first}-allday",
        "subject": "Leave / Verlof",
        "organizer": "hr@example.invalid",
        "start": {"dateTime": f"{first}T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{after}T00:00:00.0000000", "timeZone": "UTC"},
        "showAs": "oof",
        "isAllDay": True,
        "isCancelled": False,
    }
    raw.update(over)
    return raw


def jwt(expires_at: float, marker: str = "") -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at, "m": marker}).encode()).decode()
    return "h." + payload.strip("=") + ".s"


def session(**kw) -> McpSession:
    """A session with a live token, already handshaken, so tests drive `rpc`."""
    kw.setdefault("tokens", {"access_token": jwt(9e9)})
    s = McpSession(**kw)
    s._ready = True
    return s


@pytest.fixture
def served(monkeypatch):
    """Serve captured items from `search`, so the collectors run for real."""

    def _serve(items, *, notes=(), truncated=False):
        def _search(_self, _tool, **_kwargs):
            return mcp_client.SearchResult(
                items=list(items), notes=list(notes), total=len(items), truncated=truncated
            )

        monkeypatch.setattr(McpSession, "search", _search)
        return session()

    return _serve


# --- calendar --------------------------------------------------------------


def test_a_timed_event_becomes_the_raw_shape_normalize_expects():
    got = m365_mcp.normalize_event(meeting())
    assert got == {
        "subject": "!! Daily standup !!",
        "start_utc": "2026-09-01T06:45:00",
        "end_utc": "2026-09-01T07:30:00",
        "all_day": False,
        "show_as": "busy",
    }
    normalized = normalize_meetings([got], CFG, AMS)
    assert normalized[0].start.strftime("%H:%M") == "08:45"     # UTC -> local


def test_a_cancelled_event_is_dropped():
    assert m365_mcp.normalize_event(meeting(isCancelled=True)) is None


def in_zone(zone: str, raw=None) -> dict:
    raw = dict(raw or meeting())
    raw["start"] = {"dateTime": "2026-09-01T09:00:00.0000000", "timeZone": zone}
    raw["end"] = {"dateTime": "2026-09-01T10:00:00.0000000", "timeZone": zone}
    return raw


def test_a_windows_time_zone_name_is_understood():
    """Graph names zones the Windows way, which ZoneInfo rejects outright."""
    got = m365_mcp.normalize_event(in_zone("W. Europe Standard Time"))
    assert got["start_utc"] == "2026-09-01T07:00:00"            # CEST -> UTC


def test_an_unknown_time_zone_falls_back_to_utc_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        got = m365_mcp.normalize_event(in_zone("Middle Earth"))
    assert got["start_utc"] == "2026-09-01T09:00:00"
    assert "unknown time zone" in caplog.text


def test_multi_day_leave_covers_every_day_it_spans():
    raw = m365_mcp.normalize_event(all_day(first="2026-09-03", after="2026-09-08"))
    events = normalize_full_days([raw], CFG, AMS)
    assert [e.date for e in events] == [date(2026, 9, d) for d in (3, 4, 5, 6, 7)]
    assert all(e.kind == "leave" for e in events)


def test_an_all_day_event_is_never_shifted_by_a_time_zone():
    """A day-long entry on the 3rd is on the 3rd everywhere. Round-tripping its
    midnight through a zone ahead of local would move it to the 2nd, which would
    misdate every single day of leave."""
    ahead = dict(all_day())
    ahead["start"] = {"dateTime": "2026-09-03T00:00:00.0000000", "timeZone": "Asia/Tokyo"}
    ahead["end"] = {"dateTime": "2026-09-04T00:00:00.0000000", "timeZone": "Asia/Tokyo"}
    raw = m365_mcp.normalize_event(ahead)
    assert [e.date for e in normalize_full_days([raw], CFG, AMS)] == [date(2026, 9, 3)]


def test_a_one_day_all_day_event_covers_exactly_one_day():
    raw = m365_mcp.normalize_event(all_day())
    assert [e.date for e in normalize_full_days([raw], CFG, AMS)] == [date(2026, 9, 3)]


def test_an_all_day_event_with_no_end_still_covers_its_day():
    no_end = dict(all_day())
    no_end["end"] = {}
    raw = m365_mcp.normalize_event(no_end)
    assert [e.date for e in normalize_full_days([raw], CFG, AMS)] == [date(2026, 9, 3)]


def test_working_elsewhere_is_a_working_day_not_leave():
    """'workingElsewhere' is a normal day somewhere else. Mapping it to 'oof'
    would swallow the whole day as an absence."""
    raw = m365_mcp.normalize_event(
        all_day(subject="Working from the Berlin office", showAs="workingElsewhere"))
    assert raw["show_as"] == "busy"
    assert [e.kind for e in normalize_full_days([raw], CFG, AMS)] == ["meeting"]


def test_fetch_events_keeps_an_absence_that_started_before_the_window(served):
    """afterDateTime filters on when an event STARTS, so the query reaches back
    and the overlap filter is what decides, not the start."""
    s = served([all_day(first="2026-08-31", after="2026-09-05"), meeting(day="2026-09-02")])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_events(start, start + timedelta(days=7), session=s)
    assert [m["subject"] for m in got] == ["Leave / Verlof", "!! Daily standup !!"]


def test_fetch_events_drops_what_ended_before_the_window(served):
    s = served([meeting(day="2026-08-20"), meeting(day="2026-09-02")])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_events(start, start + timedelta(days=7), session=s)
    assert [m["start_utc"][:10] for m in got] == ["2026-09-02"]


def test_the_calendar_query_is_date_anchored_not_relevance_ranked(monkeypatch):
    seen = {}

    def _search(_self, _tool, **kwargs):
        seen.update(kwargs)
        return mcp_client.SearchResult(items=[], notes=[], total=0, truncated=False)

    monkeypatch.setattr(McpSession, "search", _search)
    start = datetime(2026, 9, 1, tzinfo=AMS)
    m365_mcp.fetch_events(start, start + timedelta(days=7), session=session())
    assert seen["order"] == "oldest"
    assert seen["afterDateTime"] < "2026-08-19"   # the look-back for running absences


# --- mail and chat ---------------------------------------------------------


def test_sent_mail_becomes_activity_timestamps(served):
    s = served([
        {"uri": "mail:///m/1", "id": "1", "sentDateTime": "2026-09-02T14:00:37.000Z",
         "subject": "Re: rollout"},
        {"uri": "mail:///m/2", "id": "2", "sentDateTime": "2026-09-02T14:48:23.000Z"},
    ])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_mail(start, start + timedelta(days=7), sent=True, session=s)
    assert got == [
        {"ts_utc": "2026-09-02T14:00:37", "kind": "email_sent", "subject": "Re: rollout"},
        {"ts_utc": "2026-09-02T14:48:23", "kind": "email_sent", "subject": ""},
    ]


def test_received_mail_is_tagged_apart_from_sent(served):
    """They are worth very different amounts: one is an act, the other is
    somebody else's send button."""
    s = served([{"uri": "mail:///m/3", "id": "3",
                 "receivedDateTime": "2026-09-02T08:00:00.000Z"}])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_mail(start, start + timedelta(days=7), sent=False, session=s)
    assert got[0]["kind"] == "email_received"


def test_chat_messages_from_other_people_are_not_evidence_of_your_working_day(served):
    """The search returns every participant's messages. A colleague's 22:00
    message would otherwise credit the user with an evening of work."""
    s = served([
        {"uri": "teams:///c/a", "id": "1", "from": {"displayName": "A Colleague"},
         "createdDateTime": "2026-09-02T20:11:44.233Z"},
        {"uri": "teams:///c/a", "id": "2", "from": {"displayName": "The User"},
         "createdDateTime": "2026-09-02T09:36:56.448Z"},
    ])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_chat(start, start + timedelta(days=7), sender="The User", session=s)
    assert got == [{"ts_utc": "2026-09-02T09:36:56", "kind": "chat"}]


def test_the_chat_sender_filter_is_case_insensitive(served):
    s = served([{"uri": "teams:///c/a", "id": "1", "from": {"displayName": "the user"},
                 "createdDateTime": "2026-09-02T09:00:00.000Z"}])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    assert len(m365_mcp.fetch_chat(start, start + timedelta(days=7),
                                   sender="The User", session=s)) == 1


def test_chat_is_skipped_when_the_signed_in_user_is_unknown(monkeypatch):
    """An unfiltered read would count colleagues' messages as the user's hours.
    No signal beats a wrong one."""
    monkeypatch.delenv("M365_MCP_CHAT_SENDER", raising=False)
    monkeypatch.setattr(McpSession, "me", lambda _self: {})
    start = datetime(2026, 9, 1, tzinfo=AMS)
    assert m365_mcp.fetch_chat(start, start + timedelta(days=7), session=session()) == []


# --- transport -------------------------------------------------------------


def block(payload):
    return {"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload)}


def rpc_returning(*answers):
    remaining = [{"content": [block(p) for p in a], "isError": False} for a in answers]
    seen = []

    def _rpc(_self, _method, params):
        seen.append(params.get("arguments", {}))
        return remaining.pop(0)

    _rpc.seen = seen
    return _rpc


def test_the_block_kinds_are_told_apart(monkeypatch):
    monkeypatch.setattr(McpSession, "rpc", rpc_returning([
        {"searchInfo": {"mode": "per_chat_scan"}},
        {"uri": "teams:///c/a", "id": "1"},
        {"moreResults": True, "nextOffset": 1, "totalResultCount": 9},
    ]))
    page = session().call("chat_message_search", query="*")
    assert [i["id"] for i in page.items] == ["1"]
    assert page.info["mode"] == "per_chat_scan"
    assert (page.next_offset, page.total) == (1, 9)


def test_a_prose_note_is_kept_not_dropped(monkeypatch):
    """The chat search prefixes a note when a scan was cut short. Losing it
    turns a partial answer into an apparently complete one."""
    monkeypatch.setattr(McpSession, "rpc", rpc_returning(
        ["Note: results are partial, the scan hit its time budget."]))
    page = session().call("chat_message_search", query="*")
    assert "partial" in page.notes[0]


def test_an_unrecognised_block_makes_the_answer_partial(monkeypatch):
    """Skipping it silently would make a moved payload look like a quiet week.
    Raising would let one new metadata block break every calendar fetch. It
    becomes a note, which marks the search incomplete."""
    monkeypatch.setattr(McpSession, "rpc", rpc_returning([{"somethingNew": 1}]))
    page = session().call("outlook_calendar_search")
    assert page.items == []
    assert "unrecognised" in page.notes[0]


def test_search_pages_to_the_end(monkeypatch):
    rpc = rpc_returning(
        [{"uri": "u", "id": "1"}, {"nextOffset": 1, "totalResultCount": 2}],
        [{"uri": "u", "id": "2"}, {"totalResultCount": 2}],
    )
    monkeypatch.setattr(McpSession, "rpc", rpc)
    found = session().search("outlook_calendar_search", query="*")
    assert [i["id"] for i in found.items] == ["1", "2"]
    assert [a["offset"] for a in rpc.seen] == [0, 1]
    assert found.complete


def test_pagination_that_does_not_advance_raises(monkeypatch):
    monkeypatch.setattr(McpSession, "rpc", rpc_returning(
        [{"uri": "u", "id": "1"}, {"nextOffset": 0}],
        [{"uri": "u", "id": "1"}, {"nextOffset": 0}],
    ))
    with pytest.raises(mcp_client.McpError, match="did not advance"):
        session().search("outlook_calendar_search", query="*")


def test_an_unconnected_session_will_not_stop_to_sign_in():
    """A device code blocks until a human types it. A scheduled job has none."""
    with pytest.raises(mcp_client.McpAuthError, match="sign in again"):
        McpSession().access_token()


def test_a_non_https_url_is_refused():
    """The connector URL and tenant both come from configuration, and urllib
    speaks file:// — which would turn a token request into a local file read."""
    with pytest.raises(mcp_client.McpError, match="non-https"):
        mcp_client._urlopen("file:///etc/passwd", data=b"", timeout=1)


def test_a_tenant_that_is_not_one_path_segment_is_refused():
    with pytest.raises(ValueError, match="single URL path segment"):
        mcp_client.authority("evil.example/../../x")


def test_every_request_carries_a_fresh_json_rpc_id(monkeypatch):
    seen = []

    def _capture(url, *, data, headers=None, timeout):
        seen.append(json.loads(data)["id"])
        raise mcp_client.McpError("stop here")

    monkeypatch.setattr(mcp_client, "_urlopen", _capture)
    s = session()
    for _ in range(2):
        with pytest.raises(mcp_client.McpError):
            s.rpc("tools/call", {})
    assert seen[0] != seen[1]


def test_me_survives_an_empty_get_me(monkeypatch):
    monkeypatch.setattr(McpSession, "rpc", rpc_returning([]))
    assert session().me() == {}


# --- one session per person ------------------------------------------------


def test_two_sessions_never_share_a_token(monkeypatch):
    seen = []

    def _capture(url, *, data, headers=None, timeout):
        seen.append(headers["Authorization"])
        raise mcp_client.McpError("stop")

    monkeypatch.setattr(mcp_client, "_urlopen", _capture)
    for token in ("alice", "bob"):
        s = McpSession(tokens={"access_token": jwt(9e9, token)})
        s._ready = True
        with pytest.raises(mcp_client.McpError):
            s.rpc("tools/call", {})
    assert seen[0] != seen[1]


def test_a_rotated_refresh_token_is_handed_to_the_owner(monkeypatch):
    """Entra replaces the refresh token on every use. Dropping the replacement
    works for exactly one more read and then dies, ninety days later."""
    saved = {}
    monkeypatch.setattr(mcp_client, "_post_form",
                        lambda *_a: {"access_token": jwt(9e9),
                                     "refresh_token": "the-new-one"})
    s = McpSession(tokens={"refresh_token": "the-old-one"}, on_rotate=saved.update)
    s.access_token()
    assert saved["refresh_token"] == "the-new-one"


def test_a_failing_rotate_callback_does_not_break_the_read(monkeypatch, caplog):
    def _boom(_fresh):
        raise OSError("database down")

    monkeypatch.setattr(mcp_client, "_post_form",
                        lambda *_a: {"access_token": jwt(9e9)})
    s = McpSession(tokens={"refresh_token": "old"}, on_rotate=_boom)
    with caplog.at_level(logging.WARNING):
        assert s.access_token().startswith("h.")
    assert "could not persist" in caplog.text


# --- source selection ------------------------------------------------------


def test_the_calendar_source_is_chosen_explicitly():
    connected = McpSession(tokens={"refresh_token": "r"})
    assert Sources(calendar="mcp").calendar_source == "mcp"
    assert Sources(ics_url="https://example.invalid/x.ics").calendar_source == "ics"
    assert Sources().calendar_source == "none"
    assert Sources(m365=connected).calendar_source == "mcp"
    # An explicit choice beats a leftover ICS url, so a half-finished migration
    # does not silently keep reading the old source.
    assert Sources(calendar="mcp", ics_url="x").calendar_source == "mcp"


def test_a_misspelt_source_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="calendar source"):
        Sources(calendar="MCP-connector").calendar_source


# --- transport: the failure paths ------------------------------------------
#
# These are the paths that decide whether a broken connector is loud or silent,
# which makes them the ones worth pinning down. All offline.


def _raise_oserror(*_args, **_kwargs):
    raise OSError("read-only file system")


def raiser(exc):
    def _boom(*_args, **_kwargs):
        raise exc

    return _boom


class FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.invalid", code, "err", {}, io.BytesIO(body.encode()))


def test_entras_error_body_is_returned_not_discarded(monkeypatch):
    """Entra reports OAuth failures as a 4xx with the reason in the body, so
    throwing the body away loses the only explanation there is."""
    monkeypatch.setattr(mcp_client, "_urlopen", raiser(
        http_error(400, '{"error": "invalid_grant", "error_description": "expired"}')))
    assert mcp_client._post_form("https://x.invalid", {})["error"] == "invalid_grant"


def test_a_non_json_error_body_still_raises_cleanly(monkeypatch):
    monkeypatch.setattr(mcp_client, "_urlopen", raiser(http_error(502, "<html>gateway</html>")))
    with pytest.raises(mcp_client.McpError, match="HTTP 502"):
        mcp_client._post_form("https://x.invalid", {})


def test_an_unreachable_host_raises_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(mcp_client, "_urlopen", raiser(urllib.error.URLError("no route")))
    with pytest.raises(mcp_client.McpError, match="cannot reach"):
        mcp_client._post_form("https://x.invalid", {})


def test_a_malformed_access_token_reads_as_expired():
    """Better to refresh needlessly than to send a token that cannot be parsed."""
    assert mcp_client._expiry("not-a-jwt") == 0.0
    assert mcp_client._expiry("a.!!!.c") == 0.0


def test_the_cache_is_seeded_from_a_read_only_file(monkeypatch, tmp_path):
    seed = tmp_path / "secret" / "m365.json"
    seed.parent.mkdir()
    seed.write_text('{"access_token": "x"}')
    cache = tmp_path / "run" / "token.json"
    monkeypatch.setattr(mcp_client, "CACHE", cache)
    monkeypatch.setattr(mcp_client, "SEED", str(seed))
    monkeypatch.setattr(mcp_client, "SEED_JSON", "")
    assert mcp_client._cache_path().read_text() == '{"access_token": "x"}'


def test_an_unwritable_cache_warns_but_does_not_kill_the_run(monkeypatch, tmp_path, caplog):
    """The access token in hand still works; it is the rotated refresh token
    that is lost, and that is a warning for next time, not a failure now."""
    monkeypatch.setattr(mcp_client, "CACHE", tmp_path / "token.json")
    monkeypatch.setattr(mcp_client, "SEED", "")
    monkeypatch.setattr(mcp_client, "SEED_JSON", "")
    monkeypatch.setattr(pathlib.Path, "write_text", _raise_oserror)
    with caplog.at_level(logging.WARNING):
        mcp_client._save_to_cache({"access_token": "x"})
    assert "cannot write" in caplog.text


def test_an_unreadable_cache_falls_through_instead_of_crashing(monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text("{not json")
    monkeypatch.setattr(mcp_client, "CACHE", cache)
    monkeypatch.setattr(mcp_client, "SEED", "")
    monkeypatch.setattr(mcp_client, "SEED_JSON", "")
    monkeypatch.setattr(mcp_client, "_default", None)
    assert mcp_client.default_session().tokens == {}


def test_a_rejected_refresh_is_reported_not_retried_forever(monkeypatch, caplog):
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {"error": "invalid_grant"})
    s = McpSession(tokens={"access_token": jwt(0), "refresh_token": "spent"})
    with caplog.at_level(logging.WARNING), pytest.raises(mcp_client.McpAuthError):
        s.access_token()
    assert "refresh rejected" in caplog.text


# --- the device-code flow, split for a browser -----------------------------


def test_a_device_code_is_issued_without_blocking(monkeypatch):
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {
        "user_code": "ABC-123", "verification_uri": "https://x.invalid",
        "device_code": "secret", "expires_in": 900, "interval": 5})
    pending = mcp_client.start_device_code()
    assert pending.user_code == "ABC-123"
    # The device_code is the bearer secret of the pending sign-in and must never
    # reach a page.
    assert "secret" not in json.dumps(pending.public())


def test_polling_returns_none_while_the_human_has_not_finished(monkeypatch):
    monkeypatch.setattr(mcp_client, "_post_form",
                        lambda *_a: {"error": "authorization_pending"})
    pending = DeviceCode("d", "A", "https://x.invalid", time.time() + 900, 5)
    assert mcp_client.poll_device_code(pending) is None


def test_a_real_sign_in_failure_stops_the_poll(monkeypatch):
    """'Keep polling' on a declined sign-in spins until the tab is closed."""
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {
        "error": "authorization_declined", "error_description": "the user said no"})
    pending = DeviceCode("d", "A", "https://x.invalid", time.time() + 900, 5)
    with pytest.raises(mcp_client.McpAuthError, match="said no"):
        mcp_client.poll_device_code(pending)


def test_an_expired_code_is_refused_rather_than_polled(monkeypatch):
    pending = DeviceCode("d", "A", "https://x.invalid", time.time() - 1, 5)
    with pytest.raises(mcp_client.McpAuthError, match="expired"):
        mcp_client.poll_device_code(pending)


def test_a_refused_device_code_says_why(monkeypatch):
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_a: {
        "error": "unauthorized_client", "error_description": "not preauthorized"})
    with pytest.raises(mcp_client.McpAuthError, match="not preauthorized"):
        mcp_client.start_device_code()


def rpc_transport(monkeypatch, body: str):
    monkeypatch.setattr(mcp_client, "_urlopen", lambda *a, **k: FakeResponse(body))


def test_an_sse_framed_answer_is_understood(monkeypatch):
    """The server may answer as text/event-stream even when JSON was acceptable."""
    rpc_transport(monkeypatch, 'event: message\ndata: {"result": {"ok": true}}\n\n')
    assert session().rpc("tools/list", {}) == {"ok": True}


def test_a_json_rpc_error_is_raised_with_its_message(monkeypatch):
    rpc_transport(monkeypatch, json.dumps(
        {"error": {"code": -32602, "message": "Input validation error"}}))
    with pytest.raises(mcp_client.McpError, match="Input validation error"):
        session().rpc("tools/call", {})


def test_a_non_json_body_is_not_mistaken_for_an_empty_answer(monkeypatch):
    rpc_transport(monkeypatch, "<html>502 Bad Gateway</html>")
    with pytest.raises(mcp_client.McpError, match="non-JSON"):
        session().rpc("tools/call", {})


def test_an_http_error_from_the_connector_is_raised(monkeypatch):
    monkeypatch.setattr(mcp_client, "_urlopen", raiser(
        http_error(401, "No valid issuers detected")))
    with pytest.raises(mcp_client.McpError, match="401"):
        session().rpc("tools/call", {})


def test_an_unreachable_connector_is_raised(monkeypatch):
    monkeypatch.setattr(mcp_client, "_urlopen", raiser(urllib.error.URLError("dns")))
    with pytest.raises(mcp_client.McpError, match="cannot reach"):
        session().rpc("tools/call", {})


def test_the_handshake_runs_once_per_session(monkeypatch):
    calls = []
    monkeypatch.setattr(McpSession, "rpc",
                        lambda _s, m, _p: calls.append(m) or {"tools": []})
    s = McpSession(tokens={"access_token": "t"})
    s.tools()
    s.tools()
    assert calls.count("initialize") == 1


# --- sending ---------------------------------------------------------------


def test_a_chat_message_reuses_an_existing_one_to_one_chat(monkeypatch):
    sent = {}

    def _search(_self, tool, **_kw):
        assert tool == "teams_list_chats"
        return mcp_client.SearchResult(
            items=[{"id": "chat-1", "members": [{"email": "Boss@example.invalid"}]}],
            notes=[], total=1, truncated=False)

    def _call(_self, tool, **kw):
        sent[tool] = kw
        return mcp_client.Page(items=[{"id": "chat-1"}])

    monkeypatch.setattr(McpSession, "search", _search)
    monkeypatch.setattr(McpSession, "call", _call)
    monkeypatch.setattr(McpSession, "tools", lambda _s: [
        {"name": "teams_send_chat_message",
         "inputSchema": {"properties": {"chatId": {}, "content": {}}}}])

    chat_id = m365_mcp.send_chat("boss@example.invalid", "hello", session=session())
    assert chat_id == "chat-1"
    assert "teams_create_chat" not in sent           # no second chat with the same person
    assert sent["teams_send_chat_message"] == {"chatId": "chat-1", "content": "hello"}


def test_the_send_path_adapts_to_the_connectors_own_argument_names(monkeypatch):
    """The connector is not a versioned API this project owns. Hard-coding one
    spelling breaks silently the day it changes."""
    sent = {}
    monkeypatch.setattr(McpSession, "search", lambda _s, _t, **_k: mcp_client.SearchResult(
        items=[], notes=[], total=0, truncated=False))
    def _call(_self, tool, **kw):
        sent[tool] = kw
        return mcp_client.Page(items=[{"id": "new-chat"}])

    monkeypatch.setattr(McpSession, "call", _call)
    monkeypatch.setattr(McpSession, "tools", lambda _s: [
        {"name": "teams_create_chat", "inputSchema": {"properties": {"userIds": {}}}},
        {"name": "teams_send_chat_message",
         "inputSchema": {"properties": {"conversationId": {}, "body": {}}}}])

    m365_mcp.send_chat("boss@example.invalid", "hi", session=session())
    assert sent["teams_create_chat"] == {"userIds": ["boss@example.invalid"]}
    assert sent["teams_send_chat_message"]["conversationId"] == "new-chat"
    assert sent["teams_send_chat_message"]["body"] == "hi"


def test_a_missing_send_tool_is_an_error_not_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(McpSession, "search", lambda _s, _t, **_k: mcp_client.SearchResult(
        items=[], notes=[], total=0, truncated=False))
    monkeypatch.setattr(McpSession, "tools", lambda _s: [])
    with pytest.raises(mcp_client.McpError, match="does not offer"):
        m365_mcp.send_chat("boss@example.invalid", "hi", session=session())


# --- the CLI ---------------------------------------------------------------


def test_cli_arguments_are_coerced_to_their_obvious_types():
    assert mcp_client._coerce("true") is True
    assert mcp_client._coerce("false") is False
    assert mcp_client._coerce("25") == 25
    assert mcp_client._coerce("2026-09-01") == "2026-09-01"


def test_the_cli_prints_the_tool_list(monkeypatch, capsys):
    monkeypatch.setattr(mcp_client, "tools", lambda: [
        {"name": "get_me", "inputSchema": {"properties": {}}}])
    assert mcp_client.main(["mcp_client", "tools"]) == 0
    assert "get_me" in capsys.readouterr().out


def test_the_cli_prints_one_json_object_per_item(monkeypatch, capsys):
    monkeypatch.setattr(mcp_client, "call", lambda _t, **_k: mcp_client.Page(
        items=[{"id": "1"}, {"id": "2"}]))
    assert mcp_client.main(["mcp_client", "call", "outlook_email_search", "limit=2"]) == 0
    assert capsys.readouterr().out.count('"id"') == 2


def test_an_unknown_cli_command_is_a_usage_error(capsys):
    assert mcp_client.main(["mcp_client"]) == 2
    assert "login" in capsys.readouterr().out


# --- the wiring ------------------------------------------------------------


@pytest.fixture
def mcp_week(monkeypatch):
    """Wire collect_week to the MCP collectors with nothing live behind them."""
    from timesheet.collectors import github

    monkeypatch.setattr(github, "fetch_commits", lambda *_a, **_k: [])
    monkeypatch.setattr(m365_mcp, "fetch_events", lambda *_a, **_k: [
        m365_mcp.normalize_event(meeting(day="2026-09-01")),
        m365_mcp.normalize_event(meeting(day="2026-09-02")),
    ])
    called = {"sent": 0, "received": 0, "chat": 0}

    def _mail(*_a, sent=True, **_k):
        called["sent" if sent else "received"] += 1
        # 10:00 local on the Tuesday, inside the morning block.
        return [{"ts_utc": "2026-09-01T08:00:00", "kind":
                 "email_sent" if sent else "email_received", "subject": "Re: rollout"}]

    def _chat(*_a, **_k):
        called["chat"] += 1
        return []

    monkeypatch.setattr(m365_mcp, "fetch_mail", _mail)
    monkeypatch.setattr(m365_mcp, "fetch_chat", _chat)
    return called


def _sources():
    return Sources(m365=McpSession(tokens={"refresh_token": "r"}), calendar="mcp")


def test_the_web_path_skips_the_heavy_reads(mcp_week):
    """Three extra searches is not a price a page view can pay."""
    collect_week(date(2026, 9, 1), Config(tz="Europe/Amsterdam", include_authored=False),
                 _sources(), full=False)
    assert mcp_week == {"sent": 0, "received": 0, "chat": 0}


def test_the_scheduled_refresh_reads_mail_and_chat(mcp_week):
    cfg = Config(tz="Europe/Amsterdam", include_authored=False, include_reviews=False)
    days = collect_week(date(2026, 9, 1), cfg, _sources(), full=True)
    assert mcp_week == {"sent": 1, "received": 1, "chat": 1}
    labels = [b.taak for d in days for b in d.blocks if b.kind == "email"]
    assert any("rollout" in t for t in labels)


def test_a_broken_correspondence_read_never_costs_the_week(mcp_week, monkeypatch):
    """A thin week beats no week: what cannot be read simply is not credited."""
    monkeypatch.setattr(m365_mcp, "fetch_mail", raiser(RuntimeError("connector down")))
    cfg = Config(tz="Europe/Amsterdam", include_authored=False, include_reviews=False)
    assert collect_week(date(2026, 9, 1), cfg, _sources(), full=True)
