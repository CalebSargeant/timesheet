"""Calendar, mail and chat via the Microsoft 365 MCP connector.

The richest of the three calendar sources, alongside `m365_ics` (a published ICS
link) and `m365_graph` (device-code Graph, which many tenants block). It emits
exactly the raw shapes `normalize.py` consumes, so the reconstructor never learns
where a meeting came from.

What it adds over an ICS link:

  * **Real subjects.** A published calendar set to "availability only" hides
    every one behind a bare `Busy`.
  * **No rolling three-month window**, so a backfilled month is not silently empty.
  * **Mail and chat**, which ICS cannot give at all, and which is what turns a
    day of meetings-and-no-commits from eight hours of guesswork into eight hours
    of evidence.

Every function takes an `McpSession`, so a multi-user service reads each person's
mailbox with that person's own delegated token and nothing else.

    python -m timesheet.collectors.m365_mcp 2026-09-01 2026-09-06
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import mcp_client
from .mcp_client import McpError, McpSession

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

# Graph names time zones the Windows way, which ZoneInfo does not accept.
# Anything unlisted falls back to UTC with a warning, which costs at most a
# couple of hours on a timed meeting.
_WINDOWS_ZONES = {
    "utc": "UTC",
    "w. europe standard time": "Europe/Berlin",
    "romance standard time": "Europe/Paris",
    "gmt standard time": "Europe/London",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "gtb standard time": "Europe/Bucharest",
    "e. europe standard time": "Europe/Chisinau",
    "fle standard time": "Europe/Kiev",
    "greenwich standard time": "Atlantic/Reykjavik",
    "eastern standard time": "America/New_York",
    "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "pacific standard time": "America/Los_Angeles",
    "south africa standard time": "Africa/Johannesburg",
    "india standard time": "Asia/Kolkata",
    "singapore standard time": "Asia/Singapore",
    "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo",
    "aus eastern standard time": "Australia/Sydney",
    "new zealand standard time": "Pacific/Auckland",
}


def _session(session: McpSession | None) -> McpSession:
    return session or mcp_client.default_session()


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


def fetch_events(start: datetime, end: datetime, *,
                 session: McpSession | None = None) -> list[dict]:
    """Calendar events overlapping the window, in normalize.py's raw shape.

    The query reaches a fortnight further back than the window because
    `afterDateTime` filters on when an event *starts*: a week of leave that
    began the previous Friday is otherwise not returned at all, and the days it
    covers would be reconstructed as ordinary 8h working days.
    """
    found = _session(session).search(
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


SENT_FOLDER = os.environ.get("M365_MCP_SENT_FOLDER", "Sent Items")
INBOX_FOLDER = os.environ.get("M365_MCP_INBOX_FOLDER", "Inbox")


def fetch_mail(start: datetime, end: datetime, *, sent: bool = True,
               session: McpSession | None = None) -> list[dict]:
    """Timestamps (and subjects) of mail in the window, one folder at a time.

    Sent and received are pulled separately and tagged, because they are worth
    very different amounts. A message you sent is an action with a time on it. A
    message that arrived says only that somebody else pressed send — it is
    evidence of triage at best, which is why `Config.email_received` carries a
    small lead-in and a hard daily cap.
    """
    folder = SENT_FOLDER if sent else INBOX_FOLDER
    kind = "email_sent" if sent else "email_received"
    found = _session(session).search(
        "outlook_email_search",
        # The schema forbids combining folderName with a free-text query, so
        # this is date-filtered only.
        folderName=folder,
        order="oldest",
        afterDateTime=_utc_naive(start),
        beforeDateTime=_utc_naive(end),
    )
    out = []
    for item in found.items:
        stamp = (item.get("sentDateTime") if sent else item.get("receivedDateTime")) \
            or item.get("receivedDateTime") or item.get("sentDateTime")
        if stamp:
            out.append({"ts_utc": _iso_z(stamp), "kind": kind,
                        "subject": (item.get("subject") or "").strip()})
    log.info("m365-mcp: %d %s emails", len(out), "sent" if sent else "received")
    return out


def fetch_chat(start: datetime, end: datetime, *, sender: str | None = None,
               session: McpSession | None = None) -> list[dict]:
    """Timestamps of Teams messages the user SENT in the window.

    The search returns messages from every participant, so it has to be filtered
    down to the user's own or a chatty colleague's evening becomes evidence that
    the user was working. With date filters set the connector always takes its
    per-chat scan path, where `sender` is matched against display names and
    `from.email` comes back null — so the display name is what there is, and it
    is filtered again here because the server-side filter on that path is
    documented as best-effort.

    That same scan path only reaches the most recently active chats and the
    newest messages in each, so this is a good signal for the current week and a
    thin one for a backfilled month. A thin read costs hours, not correctness:
    what it misses simply does not get credited.
    """
    sess = _session(session)
    sender = sender or os.environ.get("M365_MCP_CHAT_SENDER") or \
        sess.me().get("displayName", "")
    if not sender:
        # Without a name there is nothing to filter on, and an unfiltered read
        # would count colleagues' messages as evidence of the user's own hours.
        # No signal beats a wrong one.
        log.warning("m365-mcp: cannot tell who the signed-in user is; skipping chat")
        return []
    found = sess.search(
        "chat_message_search",
        query="*",
        sender=sender,
        afterDateTime=_utc_naive(start),
        beforeDateTime=_utc_naive(end),
    )
    out = []
    for item in found.items:
        who = ((item.get("from") or {}).get("displayName") or "").strip()
        if who.lower() != sender.strip().lower():
            continue
        stamp = item.get("createdDateTime")
        if stamp:
            out.append({"ts_utc": _iso_z(stamp), "kind": "chat"})
    log.info("m365-mcp: %d chat messages from %r", len(out), sender)
    return out


# Sending anything needs a write scope. The connector is granted READ-ONLY
# delegated permissions — Chat.Read, Mail.Read and friends, with no
# ChatMessage.Send and no Mail.Send — so a send fails with
# "FORBIDDEN: Missing scope" at the moment somebody presses the button.
# Checking up front turns that into a sentence on the settings page.
SEND_CHAT_SCOPES = ("ChatMessage.Send", "Chat.ReadWrite")
SEND_MAIL_SCOPES = ("Mail.Send",)


def granted_scopes(session: McpSession | None = None) -> list[str]:
    """The delegated permissions Entra currently grants this connector."""
    items = _session(session).call("get_granted_scopes").items
    if not items:
        return []
    scopes = items[0].get("grantedScopes") or items[0].get("scopes") or []
    return [str(s) for s in scopes]


def can_send(kind: str, session: McpSession | None = None) -> str:
    """"" if the connector may send `kind`, else what is missing.

    Never raises: an unreachable connector is reported as such rather than
    masquerading as a permissions problem.
    """
    wanted = SEND_CHAT_SCOPES if kind == "chat" else SEND_MAIL_SCOPES
    try:
        have = set(granted_scopes(session))
    except McpError as e:
        return f"could not ask Microsoft what this connection may do: {e}"
    if have & set(wanted):
        return ""
    return (f"this Microsoft connection is read-only — it grants no "
            f"{' or '.join(wanted)}, so it cannot send on your behalf")


def me(session: McpSession | None = None) -> dict:
    """The signed-in user, or {} if the connector answered with nothing."""
    return _session(session).me()


# --- sending -------------------------------------------------------------

# The connector is not a versioned API this project owns, and its argument names
# have no compatibility promise. Rather than hard-code one spelling and break the
# day it changes, the send path reads the tool's own input schema and picks
# whichever of these names it actually offers.
_ARG_ALIASES = {
    "chat_id": ("chatId", "chat_id", "conversationId", "id"),
    "content": ("content", "message", "body", "text"),
    "members": ("members", "userIds", "participants", "emails", "memberEmails"),
}


def _schema(sess: McpSession, tool: str) -> dict:
    for t in sess.tools():
        if t.get("name") == tool:
            return (t.get("inputSchema") or {}).get("properties") or {}
    raise McpError(f"the connector does not offer {tool!r}")


def _pick(props: dict, role: str) -> str:
    for name in _ARG_ALIASES[role]:
        if name in props:
            return name
    raise McpError(f"no argument for {role!r} in {sorted(props)}")


def send_chat(recipient: str, text: str, *, session: McpSession | None = None) -> str:
    """Send `text` to one person in Teams. Returns the chat id it landed in.

    Reuses an existing one-to-one chat where the connector lists one, because
    opening a second chat with the same colleague every week is both untidy and,
    on some tenants, silently rate-limited.
    """
    sess = _session(session)
    target = recipient.strip().lower()
    chat_id = ""

    try:
        for chat in sess.search("teams_list_chats", max_pages=4).items:
            people = chat.get("members") or chat.get("participants") or []
            addresses = {
                str(p.get("email") or p.get("userPrincipalName") or p).strip().lower()
                for p in people if p
            }
            if target in addresses:
                chat_id = str(chat.get("id") or chat.get("chatId") or "")
                if chat_id:
                    break
    except McpError:
        log.debug("m365-mcp: could not list chats; creating a new one", exc_info=True)

    if not chat_id:
        props = _schema(sess, "teams_create_chat")
        created = sess.call("teams_create_chat", **{_pick(props, "members"): [recipient]})
        if not created.items:
            raise McpError("teams_create_chat returned no chat")
        chat_id = str(created.items[0].get("id") or created.items[0].get("chatId") or "")
        if not chat_id:
            raise McpError("teams_create_chat returned a chat with no id")

    props = _schema(sess, "teams_send_chat_message")
    sess.call("teams_send_chat_message", **{
        _pick(props, "chat_id"): chat_id,
        _pick(props, "content"): text,
    })
    return chat_id


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    s = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=UTC)
    e = (datetime.fromisoformat(sys.argv[2]).replace(tzinfo=UTC)
         if len(sys.argv) > 2 else s + timedelta(days=7))
    print(json.dumps({
        "meetings": fetch_events(s, e),
        "emails_sent": fetch_mail(s, e, sent=True),
        "emails_received": fetch_mail(s, e, sent=False),
        "chat": fetch_chat(s, e),
    }, indent=2))
