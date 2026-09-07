"""Calendar, sent mail and Teams activity via the M365 MCP connector.

The third calendar source, alongside `m365_ics` (a published ICS link) and
`m365_graph` (device-code Graph, which this tenant blocks). It emits exactly the
raw shapes `normalize.py` and `pipeline.enrich_admin` already consume, so the
reconstructor never learns where a meeting came from.

    python -m timesheet.collectors.m365_mcp 2026-09-01 2026-09-06

It is also the answer to the Power Automate detour. `/api/ingest` exists because
an ICS link carries meetings and nothing else, so email and Teams activity had
to be pushed in from outside. Both are readable here directly, which means the
nightly refresh can enrich its own admin blocks with no flow to maintain.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import mcp_client

log = logging.getLogger(__name__)

_UTC = ZoneInfo("UTC")

# Graph's showAs vocabulary mapped onto the one normalize.py expects.
# 'workingElsewhere' is a normal working day somewhere else, not an absence, so
# it must not become 'oof' and swallow the day as leave.
_SHOW_AS = {
    "free": "free",
    "busy": "busy",
    "tentative": "tentative",
    "oof": "oof",
    "workingelsewhere": "busy",
    "unknown": "busy",
}

# Graph names time zones the Windows way, which ZoneInfo does not accept. Only
# the ones this tenant could plausibly produce; anything else falls back to UTC
# with a warning, which costs at most a couple of hours on a timed meeting.
_WINDOWS_ZONES = {
    "utc": "UTC",
    "w. europe standard time": "Europe/Berlin",
    "romance standard time": "Europe/Paris",
    "gmt standard time": "Europe/London",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "gtb standard time": "Europe/Bucharest",
}


def _zone(name: str) -> ZoneInfo:
    if not name:
        return _UTC
    for candidate in (name, _WINDOWS_ZONES.get(name.strip().lower(), "")):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    log.warning("m365-mcp: unknown time zone %r; reading it as UTC", name)
    return _UTC


def _wall_clock(value: str) -> datetime:
    """Graph's wall-clock string carries seven fractional digits; fromisoformat
    takes three or six. Nothing here is decided below the second."""
    text = value.strip().rstrip("Z")
    if "." in text:
        text = text.split(".", 1)[0]
    return datetime.fromisoformat(text)


def _utc_naive(dt: datetime) -> str:
    return dt.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _iso_z(value: str) -> str:
    """`2026-09-02T14:00:37.000Z` -> the naive-UTC form the pipeline parses."""
    return _utc_naive(_wall_clock(value).replace(tzinfo=_UTC))


def normalize_event(raw: dict) -> dict | None:
    """One connector event -> the raw meeting dict normalize.py consumes.

    All-day bounds are taken **literally** and stamped as UTC midnight rather
    than converted out of the event's stated zone. A day-long entry on the 3rd
    is on the 3rd everywhere, and round-tripping its midnight through a zone
    ahead of local moves it to the 2nd — which would misdate every day of leave.
    That matches how `m365_ics` treats a bare ICS DATE, so `normalize_full_days`
    behaves identically for both sources.
    """
    if raw.get("isCancelled"):
        return None
    start, end = raw.get("start") or {}, raw.get("end") or {}
    if not start.get("dateTime"):
        return None

    all_day = bool(raw.get("isAllDay"))
    if all_day:
        first = _wall_clock(start["dateTime"]).date()
        last = _wall_clock(end["dateTime"]).date() if end.get("dateTime") else first
        start_utc = f"{first.isoformat()}T00:00:00"
        end_utc = f"{max(last, first + timedelta(days=1)).isoformat()}T00:00:00"
    else:
        s = _wall_clock(start["dateTime"]).replace(tzinfo=_zone(start.get("timeZone", "")))
        e_raw = end.get("dateTime") or start["dateTime"]
        e = _wall_clock(e_raw).replace(tzinfo=_zone(end.get("timeZone", "")))
        start_utc, end_utc = _utc_naive(s), _utc_naive(e)

    return {
        "subject": raw.get("subject") or "",
        "start_utc": start_utc,
        "end_utc": end_utc,
        "all_day": all_day,
        "show_as": _SHOW_AS.get((raw.get("showAs") or "busy").strip().lower(), "busy"),
    }


def fetch_events(start: datetime, end: datetime) -> list[dict]:
    """Calendar events overlapping the window, in normalize.py's raw shape.

    The query reaches a fortnight further back than the window because
    `afterDateTime` filters on when an event *starts*: a week of leave that
    began the previous Friday is otherwise not returned at all, and the days it
    covers would be reconstructed as ordinary 8h working days.
    """
    found = mcp_client.search(
        "outlook_calendar_search",
        query="*",
        # Anchors the search to the date range instead of relevance-ranking it,
        # which is the difference between "every event in the window" and "the
        # 25 events the ranker liked most".
        order="oldest",
        afterDateTime=_utc_naive(start - timedelta(days=14)),
        beforeDateTime=_utc_naive(end + timedelta(days=1)),
    )
    out = [m for m in (normalize_event(e) for e in found.items) if m]
    # Keep anything that overlaps the window, not just what starts inside it.
    lo, hi = _utc_naive(start), _utc_naive(end)
    out = [m for m in out if m["end_utc"] > lo and m["start_utc"] < hi]
    out.sort(key=lambda m: m["start_utc"])
    log.info("m365-mcp: %d calendar events for %s..%s", len(out), lo[:10], hi[:10])
    return out


def fetch_emails(start: datetime, end: datetime) -> list[dict]:
    """Timestamps of mail the user SENT in the window.

    Sent, not received: `enrich_admin` reads these as evidence that the user was
    at their desk working, and mail arrives whether or not they were. A sent
    message is an action with a time on it.
    """
    found = mcp_client.search(
        "outlook_email_search",
        # The schema forbids combining folderName with a free-text query, so
        # this is date-filtered only.
        folderName=os.environ.get("M365_MCP_SENT_FOLDER", "Sent Items"),
        order="oldest",
        afterDateTime=_utc_naive(start),
        beforeDateTime=_utc_naive(end),
    )
    out = []
    for item in found.items:
        stamp = item.get("sentDateTime") or item.get("receivedDateTime")
        if stamp:
            out.append({"ts_utc": _iso_z(stamp)})
    log.info("m365-mcp: %d sent emails", len(out))
    return out


def fetch_teams(start: datetime, end: datetime, *, sender: str | None = None) -> list[dict]:
    """Timestamps of Teams messages the user SENT in the window.

    The search returns messages from every participant, so it has to be filtered
    down to the user's own or a chatty colleague's evening becomes evidence that
    the user was working. With date filters set the connector always takes its
    per-chat scan path, where `sender` is matched against display names and
    `from.email` comes back null — so the display name is what there is, and it
    is filtered again here because the server-side filter on that path is
    documented as best-effort.

    That same scan path only reaches the 50 most recently active chats and the
    50 newest messages in each, so this is a good signal for the current week
    and a thin one for a backfilled month. It only ever relabels admin blocks,
    never their durations, so a thin read costs nothing but a generic label.
    """
    sender = sender or os.environ.get("M365_MCP_TEAMS_SENDER") or me().get("displayName", "")
    found = mcp_client.search(
        "chat_message_search",
        query="*",
        sender=sender,
        afterDateTime=_utc_naive(start),
        beforeDateTime=_utc_naive(end),
    )
    out = []
    for item in found.items:
        who = ((item.get("from") or {}).get("displayName") or "").strip()
        if sender and who and who.lower() != sender.strip().lower():
            continue
        stamp = item.get("createdDateTime")
        if stamp:
            out.append({"ts_utc": _iso_z(stamp)})
    log.info("m365-mcp: %d Teams messages from %r", len(out), sender)
    return out


def me() -> dict:
    return mcp_client.call("get_me").items[0]


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    s = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=UTC)
    e = (datetime.fromisoformat(sys.argv[2]).replace(tzinfo=UTC)
         if len(sys.argv) > 2 else s + timedelta(days=7))
    print(json.dumps({
        "meetings": fetch_events(s, e),
        "emails": fetch_emails(s, e),
        "teams": fetch_teams(s, e),
    }, indent=2))
