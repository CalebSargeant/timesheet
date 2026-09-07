"""The MCP collector, against captured connector payloads. No network.

Shapes here came from live `outlook_calendar_search` / `outlook_email_search` /
`chat_message_search` responses, stripped of anything identifying. They exist so
a change in the connector's output is caught here rather than by a sheet that
quietly reconstructs a week of leave as eight-hour working days.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timesheet.collectors import m365_mcp, mcp_client, normalize_full_days, normalize_meetings
from timesheet.config import Config
from timesheet.pipeline import calendar_source

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


def test_an_unrecognised_block_raises_rather_than_being_skipped(monkeypatch):
    monkeypatch.setattr(mcp_client, "_rpc", rpc_returning([{"somethingNew": 1}]))
    with pytest.raises(mcp_client.McpError, match="unrecognised"):
        mcp_client.call("outlook_calendar_search")


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
