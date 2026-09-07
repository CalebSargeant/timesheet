"""The MCP collector, against captured connector payloads. No network.

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
import urllib.error
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timesheet.collectors import m365_mcp, mcp_client, normalize_full_days, normalize_meetings
from timesheet.config import Config
from timesheet.pipeline import calendar_source, collect_week

UTC = ZoneInfo("UTC")
AMS = ZoneInfo("Europe/Amsterdam")


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


@pytest.fixture
def served(monkeypatch):
    """Serve captured items from `search`, so the collectors run for real."""

    def _serve(items, *, notes=(), truncated=False):
        def _search(_tool, **_kwargs):
            return mcp_client.SearchResult(
                items=list(items), notes=list(notes), total=len(items), truncated=truncated
            )

        monkeypatch.setattr(mcp_client, "search", _search)
        monkeypatch.setattr(m365_mcp.mcp_client, "search", _search)

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
    # And it survives the existing normaliser at the right local time.
    (m,) = normalize_meetings([got], Config(), AMS)
    assert m.start == datetime(2026, 9, 1, 8, 45, tzinfo=AMS)
    assert m.taak == "Daily standup"


def test_a_cancelled_event_is_dropped():
    assert m365_mcp.normalize_event(meeting(isCancelled=True)) is None


def test_a_windows_time_zone_name_is_understood():
    """Graph names zones the Windows way, which ZoneInfo rejects. 08:45 in
    W. Europe is 06:45 UTC; reading the zone as UTC would move the meeting."""
    local = meeting(start="08:45:00", end="09:30:00")
    local["start"]["timeZone"] = "W. Europe Standard Time"
    local["end"]["timeZone"] = "W. Europe Standard Time"
    assert m365_mcp.normalize_event(local)["start_utc"] == "2026-09-01T06:45:00"


def test_multi_day_leave_covers_every_day_it_spans():
    """The expensive one. A week of leave is ONE all-day object running to the
    following Monday. Reading only its start leaves four days to be
    reconstructed as ordinary 8h working days on a sheet the manager reads."""
    raw = m365_mcp.normalize_event(all_day(first="2026-09-07", after="2026-09-12"))
    covered = [e.date.isoformat() for e in normalize_full_days([raw], Config(), AMS)]
    assert covered == [f"2026-09-{d:02d}" for d in (7, 8, 9, 10, 11)]


def test_an_all_day_event_is_never_shifted_by_a_time_zone():
    """A day-long entry on the 3rd is on the 3rd everywhere. Round-tripping its
    midnight out of a zone ahead of local would move it to the 2nd."""
    leave = all_day()
    leave["start"]["timeZone"] = "Tokyo Standard Time"
    leave["end"]["timeZone"] = "Tokyo Standard Time"
    (day,) = normalize_full_days([m365_mcp.normalize_event(leave)], Config(), AMS)
    assert day.date.isoformat() == "2026-09-03"
    assert day.kind == "leave"


def test_a_one_day_all_day_event_covers_exactly_one_day():
    """The end is exclusive: 3 Sept 00:00 to 4 Sept 00:00 is one day."""
    assert len(normalize_full_days([m365_mcp.normalize_event(all_day())], Config(), AMS)) == 1


def test_an_all_day_event_with_no_end_still_covers_its_day():
    lonely = all_day()
    del lonely["end"]
    (day,) = normalize_full_days([m365_mcp.normalize_event(lonely)], Config(), AMS)
    assert day.date.isoformat() == "2026-09-03"


def test_working_elsewhere_is_a_working_day_not_leave():
    """'workingElsewhere' mapped to oof would swallow a normal day as leave."""
    assert m365_mcp.normalize_event(meeting(showAs="workingElsewhere"))["show_as"] == "busy"


def test_fetch_events_keeps_an_absence_that_started_before_the_window(served):
    """afterDateTime filters on when an event STARTS, so the query reaches back
    and the overlap filter is what decides, not the start."""
    served([all_day(first="2026-08-31", after="2026-09-05"), meeting(day="2026-09-02")])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_events(start, start + timedelta(days=7))
    assert [m["subject"] for m in got] == ["Leave / Verlof", "!! Daily standup !!"]


def test_fetch_events_drops_what_ended_before_the_window(served):
    served([meeting(day="2026-08-20"), meeting(day="2026-09-02")])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_events(start, start + timedelta(days=7))
    assert [m["start_utc"][:10] for m in got] == ["2026-09-02"]


def test_the_calendar_query_is_date_anchored_not_relevance_ranked(monkeypatch):
    seen = {}

    def _search(_tool, **kwargs):
        seen.update(kwargs)
        return mcp_client.SearchResult(items=[], notes=[], total=0, truncated=False)

    monkeypatch.setattr(m365_mcp.mcp_client, "search", _search)
    start = datetime(2026, 9, 1, tzinfo=AMS)
    m365_mcp.fetch_events(start, start + timedelta(days=7))
    assert seen["order"] == "oldest"
    assert seen["afterDateTime"] < "2026-08-19"   # the look-back for running absences


# --- email and Teams -------------------------------------------------------


def test_sent_mail_becomes_activity_timestamps(served):
    served([
        {"uri": "mail:///m/1", "id": "1", "sentDateTime": "2026-09-02T14:00:37.000Z"},
        {"uri": "mail:///m/2", "id": "2", "sentDateTime": "2026-09-02T14:48:23.000Z"},
    ])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_emails(start, start + timedelta(days=7))
    assert got == [{"ts_utc": "2026-09-02T14:00:37"}, {"ts_utc": "2026-09-02T14:48:23"}]


def test_teams_messages_from_other_people_are_not_evidence_of_your_working_day(served):
    """The search returns every participant's messages. A colleague's 22:00
    message would otherwise relabel an evening block as the user's own work."""
    served([
        {"uri": "teams:///c/a", "id": "1", "from": {"displayName": "A Colleague"},
         "createdDateTime": "2026-09-02T20:11:44.233Z"},
        {"uri": "teams:///c/a", "id": "2", "from": {"displayName": "The User"},
         "createdDateTime": "2026-09-02T09:36:56.448Z"},
    ])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    got = m365_mcp.fetch_teams(start, start + timedelta(days=7), sender="The User")
    assert got == [{"ts_utc": "2026-09-02T09:36:56"}]


def test_the_teams_sender_filter_is_case_insensitive(served):
    served([{"uri": "teams:///c/a", "id": "1", "from": {"displayName": "the user"},
             "createdDateTime": "2026-09-02T09:00:00.000Z"}])
    start = datetime(2026, 9, 1, tzinfo=AMS)
    assert len(m365_mcp.fetch_teams(start, start + timedelta(days=7), sender="The User")) == 1


# --- transport -------------------------------------------------------------


def block(payload):
    return {"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload)}


def rpc_returning(*answers):
    remaining = [{"content": [block(p) for p in a], "isError": False} for a in answers]
    seen = []

    def _rpc(_method, params):
        seen.append(params.get("arguments", {}))
        return remaining.pop(0)

    _rpc.seen = seen
    return _rpc


@pytest.fixture(autouse=True)
def _no_handshake(monkeypatch):
    monkeypatch.setattr(mcp_client, "_ready", True)


def test_the_block_kinds_are_told_apart(monkeypatch):
    monkeypatch.setattr(mcp_client, "_rpc", rpc_returning([
        {"searchInfo": {"mode": "per_chat_scan"}},
        {"uri": "teams:///c/a", "id": "1"},
        {"moreResults": True, "nextOffset": 1, "totalResultCount": 9},
    ]))
    page = mcp_client.call("chat_message_search", query="*")
    assert [i["id"] for i in page.items] == ["1"]
    assert page.info["mode"] == "per_chat_scan"
    assert (page.next_offset, page.total) == (1, 9)


def test_a_prose_note_is_kept_not_dropped(monkeypatch):
    """The chat search prefixes a note when a scan was cut short. Losing it
    turns a partial answer into an apparently complete one."""
    monkeypatch.setattr(mcp_client, "_rpc", rpc_returning(
        ["Note: results are partial, the scan hit its time budget."]))
    page = mcp_client.call("chat_message_search", query="*")
    assert "partial" in page.notes[0]


def test_an_unrecognised_block_makes_the_answer_partial(monkeypatch):
    """Skipping it silently would make a moved payload look like a quiet week.
    Raising would let one new metadata block break every calendar fetch. It
    becomes a note, which marks the search incomplete."""
    monkeypatch.setattr(mcp_client, "_rpc", rpc_returning([{"somethingNew": 1}]))
    page = mcp_client.call("outlook_calendar_search")
    assert page.items == []
    assert "unrecognised" in page.notes[0]


def test_search_pages_to_the_end(monkeypatch):
    rpc = rpc_returning(
        [{"uri": "u", "id": "1"}, {"nextOffset": 1, "totalResultCount": 2}],
        [{"uri": "u", "id": "2"}, {"totalResultCount": 2}],
    )
    monkeypatch.setattr(mcp_client, "_rpc", rpc)
    found = mcp_client.search("outlook_calendar_search", query="*")
    assert [i["id"] for i in found.items] == ["1", "2"]
    assert [a["offset"] for a in rpc.seen] == [0, 1]
    assert found.complete


def test_pagination_that_does_not_advance_raises(monkeypatch):
    monkeypatch.setattr(mcp_client, "_rpc", rpc_returning(
        [{"uri": "u", "id": "1"}, {"nextOffset": 0}],
        [{"uri": "u", "id": "1"}, {"nextOffset": 0}],
    ))
    with pytest.raises(mcp_client.McpError, match="did not advance"):
        mcp_client.search("outlook_calendar_search", query="*")


def test_a_cron_run_will_not_stop_to_sign_in(monkeypatch, tmp_path):
    """A device code blocks until a human types it. The nightly job has none."""
    monkeypatch.setattr(mcp_client, "CACHE", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_client, "SEED", "")
    monkeypatch.setattr(mcp_client, "SEED_JSON", "")
    with pytest.raises(mcp_client.McpAuthError, match="login"):
        mcp_client.token()


def test_the_token_can_be_seeded_from_an_env_var(monkeypatch, tmp_path):
    import base64
    import time

    exp = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
    monkeypatch.setattr(mcp_client, "CACHE", tmp_path / "run" / "token.json")
    monkeypatch.setattr(mcp_client, "SEED", "")
    monkeypatch.setattr(mcp_client, "SEED_JSON", json.dumps(
        {"access_token": f"h.{exp.decode().strip('=')}.s"}))
    assert mcp_client.token().startswith("h.")


# --- source selection ------------------------------------------------------


def test_the_calendar_source_is_chosen_explicitly():
    assert calendar_source({"M365_SOURCE": "mcp"}) == "mcp"
    assert calendar_source({"M365_ICS_URL": "https://example.invalid/x.ics"}) == "ics"
    assert calendar_source({}) == "graph"
    # An explicit choice beats a leftover ICS url, so a half-finished migration
    # does not silently keep reading the old source.
    assert calendar_source({"M365_SOURCE": "mcp", "M365_ICS_URL": "x"}) == "mcp"


def test_a_misspelt_source_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="M365_SOURCE"):
        calendar_source({"M365_SOURCE": "MCP-connector"})


def test_a_non_https_url_is_refused():
    """M365_MCP_URL and M365_MCP_TENANT both come from the environment, and
    urllib speaks file:// — which would turn a token request into a file read."""
    with pytest.raises(mcp_client.McpError, match="non-https"):
        mcp_client._urlopen("file:///etc/passwd", data=b"", timeout=1)


def test_every_request_carries_a_fresh_json_rpc_id(monkeypatch):
    seen = []

    def _capture(url, *, data, headers=None, timeout):
        seen.append(json.loads(data)["id"])
        raise mcp_client.McpError("stop here")

    monkeypatch.setattr(mcp_client, "_urlopen", _capture)
    monkeypatch.setattr(mcp_client, "token", lambda **_: "t")
    for _ in range(2):
        with pytest.raises(mcp_client.McpError):
            mcp_client._rpc("tools/call", {})
    assert seen[0] != seen[1]


def test_teams_is_skipped_when_the_signed_in_user_is_unknown(monkeypatch):
    """An unfiltered read would count colleagues' messages as the user's hours.
    No signal beats a wrong one."""
    monkeypatch.setattr(m365_mcp, "me", dict)
    monkeypatch.delenv("M365_MCP_TEAMS_SENDER", raising=False)
    start = datetime(2026, 9, 1, tzinfo=AMS)
    assert m365_mcp.fetch_teams(start, start + timedelta(days=7)) == []


def test_me_survives_an_empty_get_me(monkeypatch):
    monkeypatch.setattr(mcp_client, "_rpc", rpc_returning([]))
    monkeypatch.setattr(m365_mcp.mcp_client, "_rpc", rpc_returning([]))
    assert m365_mcp.me() == {}


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


def jwt(expires_at: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires_at}).encode()).decode()
    return "h." + payload.strip("=") + ".s"


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
        mcp_client._save({"access_token": "x"})
    assert "cannot write" in caplog.text


def test_an_unreadable_cache_falls_through_instead_of_crashing(monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text("{not json")
    monkeypatch.setattr(mcp_client, "CACHE", cache)
    monkeypatch.setattr(mcp_client, "SEED", "")
    monkeypatch.setattr(mcp_client, "SEED_JSON", "")
    with pytest.raises(mcp_client.McpAuthError):
        mcp_client.token()


def test_a_rejected_refresh_falls_back_to_a_fresh_sign_in(monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text(json.dumps({"access_token": jwt(0), "refresh_token": "spent"}))
    monkeypatch.setattr(mcp_client, "CACHE", cache)
    monkeypatch.setattr(mcp_client, "SEED", "")
    monkeypatch.setattr(mcp_client, "SEED_JSON", "")
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_: {"error": "invalid_grant"})
    monkeypatch.setattr(mcp_client, "_device_code", lambda: {"access_token": jwt(9e9)})
    assert mcp_client.token(allow_device_code=True).startswith("h.")


def test_the_device_code_flow_polls_until_the_user_finishes(monkeypatch, capsys):
    answers = [
        {"user_code": "ABC-123", "verification_uri": "https://x.invalid", "device_code": "d",
         "expires_in": 900, "interval": 1},
        {"error": "authorization_pending"},
        {"error": "slow_down"},
        {"access_token": jwt(9e9)},
    ]
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_: answers.pop(0))
    monkeypatch.setattr(mcp_client.time, "sleep", lambda _s: None)
    assert mcp_client._device_code()["access_token"].startswith("h.")
    assert "ABC-123" in capsys.readouterr().err


def test_a_refused_device_code_says_why(monkeypatch):
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_: {
        "error": "unauthorized_client", "error_description": "not preauthorized"})
    with pytest.raises(mcp_client.McpAuthError, match="not preauthorized"):
        mcp_client._device_code()


def test_a_real_sign_in_failure_stops_the_poll(monkeypatch):
    answers = [
        {"user_code": "A", "verification_uri": "https://x.invalid", "device_code": "d",
         "expires_in": 900, "interval": 1},
        {"error": "expired_token", "error_description": "the code expired"},
    ]
    monkeypatch.setattr(mcp_client, "_post_form", lambda *_: answers.pop(0))
    monkeypatch.setattr(mcp_client.time, "sleep", lambda _s: None)
    with pytest.raises(mcp_client.McpAuthError, match="expired"):
        mcp_client._device_code()


def rpc_transport(monkeypatch, body: str):
    monkeypatch.setattr(mcp_client, "token", lambda **_: "t")
    monkeypatch.setattr(mcp_client, "_urlopen", lambda *a, **k: FakeResponse(body))


def test_an_sse_framed_answer_is_understood(monkeypatch):
    """The server may answer as text/event-stream even when JSON was acceptable."""
    rpc_transport(monkeypatch, 'event: message\ndata: {"result": {"ok": true}}\n\n')
    assert mcp_client._rpc("tools/list", {}) == {"ok": True}


def test_a_json_rpc_error_is_raised_with_its_message(monkeypatch):
    rpc_transport(monkeypatch, json.dumps(
        {"error": {"code": -32602, "message": "Input validation error"}}))
    with pytest.raises(mcp_client.McpError, match="Input validation error"):
        mcp_client._rpc("tools/call", {})


def test_a_non_json_body_is_not_mistaken_for_an_empty_answer(monkeypatch):
    rpc_transport(monkeypatch, "<html>502 Bad Gateway</html>")
    with pytest.raises(mcp_client.McpError, match="non-JSON"):
        mcp_client._rpc("tools/call", {})


def test_an_http_error_from_the_connector_is_raised(monkeypatch):
    monkeypatch.setattr(mcp_client, "token", lambda **_: "t")
    monkeypatch.setattr(mcp_client, "_urlopen", raiser(
        http_error(401, "No valid issuers detected")))
    with pytest.raises(mcp_client.McpError, match="401"):
        mcp_client._rpc("tools/call", {})


def test_an_unreachable_connector_is_raised(monkeypatch):
    monkeypatch.setattr(mcp_client, "token", lambda **_: "t")
    monkeypatch.setattr(mcp_client, "_urlopen", raiser(urllib.error.URLError("dns")))
    with pytest.raises(mcp_client.McpError, match="cannot reach"):
        mcp_client._rpc("tools/call", {})


def test_the_handshake_runs_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_client, "_ready", False)
    monkeypatch.setattr(mcp_client, "_rpc", lambda m, _p: calls.append(m) or {"tools": []})
    mcp_client.tools()
    mcp_client.tools()
    assert calls.count("initialize") == 1


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


def test_the_cli_reports_a_successful_login(monkeypatch, capsys):
    monkeypatch.setattr(mcp_client, "token", lambda **_: "t")
    monkeypatch.setattr(mcp_client, "call", lambda _t, **_k: mcp_client.Page(
        items=[{"displayName": "The User", "jobTitle": "Engineer"}]))
    assert mcp_client.main(["mcp_client", "login"]) == 0
    assert "The User" in capsys.readouterr().out


def test_an_unknown_cli_command_is_a_usage_error(capsys):
    assert mcp_client.main(["mcp_client"]) == 2
    assert "login" in capsys.readouterr().out


# --- the wiring ------------------------------------------------------------


@pytest.fixture
def mcp_week(monkeypatch):
    """Wire collect_week to the MCP collectors with nothing live behind them."""
    from timesheet.collectors import ghe

    monkeypatch.setenv("M365_SOURCE", "mcp")
    monkeypatch.setattr(ghe, "fetch_commits", lambda *_a, **_k: [])
    monkeypatch.setattr(m365_mcp, "fetch_events", lambda *_a: [
        m365_mcp.normalize_event(meeting(day="2026-09-01")),
        m365_mcp.normalize_event(meeting(day="2026-09-02")),
    ])
    called = {"emails": 0, "teams": 0}

    def _emails(*_a):
        called["emails"] += 1
        # 10:00 local on the Tuesday, inside the morning admin block.
        return [{"ts_utc": "2026-09-01T08:00:00"}]

    def _teams(*_a, **_k):
        called["teams"] += 1
        return []

    monkeypatch.setattr(m365_mcp, "fetch_emails", _emails)
    monkeypatch.setattr(m365_mcp, "fetch_teams", _teams)
    return called


def test_the_web_path_skips_enrichment(mcp_week):
    """Two extra searches is not a price a page view can pay."""
    collect_week(date(2026, 9, 1), Config(include_authored=False), full=False)
    assert mcp_week == {"emails": 0, "teams": 0}


def test_the_nightly_refresh_relabels_admin_blocks_from_real_activity(mcp_week):
    days = collect_week(date(2026, 9, 1), Config(include_authored=False, include_reviews=False),
                        full=True)
    assert mcp_week == {"emails": 1, "teams": 1}
    labels = [b.taak for d in days for b in d.blocks if b.kind == "admin"]
    assert "Mail" in labels


def test_a_broken_enrichment_never_costs_the_week(mcp_week, monkeypatch):
    """It only ever relabels admin blocks. A generic label is honest; losing an
    otherwise-correct week of reconstruction over one is not."""
    monkeypatch.setattr(m365_mcp, "fetch_emails", raiser(RuntimeError("connector down")))
    days = collect_week(date(2026, 9, 1), Config(include_authored=False, include_reviews=False),
                        full=True)
    assert days
