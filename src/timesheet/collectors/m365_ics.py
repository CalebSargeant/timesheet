"""Microsoft 365 calendar via a published ICS link — no Power Automate, no app.

Outlook can publish a calendar as a secret `.ics` URL (OWA → Settings → Calendar →
Shared calendars → Publish). This collector fetches and parses it into the same raw
meeting shape the rest of the pipeline expects. Recurring events (your daily
standup) are expanded across the requested window.

    python -m timesheet.collectors.m365_ics "<ics-url>" 2026-07-20 2026-07-25
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..net import check as check_url
from ..net import open_url

_UTC = ZoneInfo("UTC")

_BUSY = {"BUSY": "busy", "FREE": "free", "TENTATIVE": "tentative", "OOF": "oof",
         "WORKINGELSEWHERE": "busy"}


def _to_utc_naive(value) -> tuple[str, bool]:
    """(iso-utc-without-offset, is_all_day). A bare date means an all-day event."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_UTC)
        return dt.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%S"), False
    if isinstance(value, date):
        midnight = datetime(value.year, value.month, value.day, tzinfo=_UTC)
        return midnight.strftime("%Y-%m-%dT%H:%M:%S"), True
    raise TypeError(f"unexpected DTSTART/DTEND type: {type(value)!r}")


def fetch_events(ics_url: str, start: datetime, end: datetime) -> list[dict]:
    # Checked before anything else happens. The URL comes from a settings form,
    # so it is precisely the case net.py exists for: `file:///etc/passwd` would
    # otherwise be read off disk and parsed as a calendar.
    check_url(ics_url)

    import icalendar
    import recurring_ical_events

    with open_url(ics_url, timeout=30) as r:
        cal = icalendar.Calendar.from_ical(r.read())

    out: list[dict] = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        try:
            s_val = ev["DTSTART"].dt
            e_val = ev.get("DTEND", ev["DTSTART"]).dt
            start_utc, all_day = _to_utc_naive(s_val)
            end_utc, _ = _to_utc_naive(e_val)
        except (KeyError, TypeError):
            continue
        busy = str(ev.get("X-MICROSOFT-CDO-BUSYSTATUS", "BUSY")).upper()
        all_day = all_day or str(ev.get("X-MICROSOFT-CDO-ALLDAYEVENT", "")).upper() == "TRUE"
        out.append({
            "subject": str(ev.get("SUMMARY", "")),
            "start_utc": start_utc,
            "end_utc": end_utc,
            "all_day": all_day,
            "show_as": _BUSY.get(busy, "busy"),
        })
    out.sort(key=lambda m: m["start_utc"])
    return out


if __name__ == "__main__":
    url = sys.argv[1]
    s = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=_UTC)
    e = (datetime.fromisoformat(sys.argv[3]).replace(tzinfo=_UTC)
         if len(sys.argv) > 3 else s + timedelta(days=7))
    print(json.dumps(fetch_events(url, s, e), indent=2))
