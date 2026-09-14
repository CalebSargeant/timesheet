"""Microsoft 365 calendar via Graph — WITHOUT registering an Azure AD app.

Auth uses the OAuth 2.0 device-authorization grant against a Microsoft *first-party*
public client (default: 'Microsoft Graph PowerShell', 14d82eec-…), which already
exists in every tenant. You sign in once; the refresh token is cached and refreshed
headlessly by the cron. No admin, no app registration.

    python -m timesheet.collectors.m365_graph login    # one-time, interactive
    python -m timesheet.collectors.m365_graph pull 2026-07-20 2026-07-25

Many tenants block this path outright (`AADSTS65002` / `700016`). Where they do,
use the MCP connector (`m365_mcp.py`, the richest source) or the ICS-publish
fallback (`m365_ics.py` / `M365_ICS_URL`).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
# Microsoft first-party public clients. Graph PowerShell (14d82eec-…) is the tidy
# default, but it is NOT provisioned in every tenant (AADSTS700016) — the Azure
# CLI client (04b07795-…) is far more widely present and is the working default
# here. Override with M365_CLIENT_ID.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"   # Microsoft Azure CLI
DEFAULT_SCOPES = ["Calendars.Read", "User.Read"]


def _tenant() -> str:
    """The authority segment: an explicit tenant, the domain of a configured UPN,
    or 'organizations' so Entra resolves the signer's own home tenant."""
    explicit = os.environ.get("M365_TENANT")
    if explicit:
        return explicit
    upn = os.environ.get("M365_UPN", "")
    return upn.split("@", 1)[1] if "@" in upn else "organizations"


def _cache_path() -> Path:
    return Path(os.environ.get("M365_TOKEN_CACHE", "m365.token.json"))


def _build_app(client_id: str):
    import msal
    cache = msal.SerializableTokenCache()
    p = _cache_path()
    if p.exists():
        cache.deserialize(p.read_text())
    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{_tenant()}",
        token_cache=cache,
    )
    return app, cache


def _save(cache):
    if cache.has_state_changed:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cache.serialize())
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def login(client_id: str | None = None, scopes: list[str] | None = None) -> dict:
    """Interactive one-time device-code login. Prints the code, blocks until done."""
    app, cache = _build_app(client_id or os.environ.get("M365_CLIENT_ID", DEFAULT_CLIENT_ID))
    flow = app.initiate_device_flow(scopes=scopes or DEFAULT_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow init failed: {json.dumps(flow, indent=2)}")
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)     # blocks, polling
    _save(cache)
    if "access_token" not in result:
        raise RuntimeError(f"login failed: {result.get('error')}: {result.get('error_description')}")
    return result


def initiate(client_id: str | None = None, scopes: list[str] | None = None) -> dict:
    """Start the device flow and persist it (for a separate poll step). Non-blocking."""
    app, _ = _build_app(client_id or os.environ.get("M365_CLIENT_ID", DEFAULT_CLIENT_ID))
    flow = app.initiate_device_flow(scopes=scopes or DEFAULT_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow init failed: {json.dumps(flow, indent=2)}")
    Path(os.environ.get("M365_FLOW", "m365_flow.json")).write_text(json.dumps(flow))
    return flow


def poll(client_id: str | None = None, scopes: list[str] | None = None) -> dict:
    """Poll a previously-initiated device flow to completion; save the token cache."""
    app, cache = _build_app(client_id or os.environ.get("M365_CLIENT_ID", DEFAULT_CLIENT_ID))
    flow = json.loads(Path(os.environ.get("M365_FLOW", "m365_flow.json")).read_text())
    result = app.acquire_token_by_device_flow(flow)     # blocks, polling
    _save(cache)
    if "access_token" not in result:
        raise RuntimeError(f"login failed: {result.get('error')}: {result.get('error_description')}")
    return result


def get_token(client_id: str | None = None, scopes: list[str] | None = None) -> str:
    """Headless: refresh silently from the cached account. Raises if not logged in."""
    app, cache = _build_app(client_id or os.environ.get("M365_CLIENT_ID", DEFAULT_CLIENT_ID))
    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError("no cached M365 account — run `m365_graph login` once")
    result = app.acquire_token_silent(scopes or DEFAULT_SCOPES, account=accounts[0])
    _save(cache)
    if not result or "access_token" not in result:
        raise RuntimeError("silent token refresh failed — re-run `m365_graph login`")
    return result["access_token"]


def _graph_get(path: str, token: str, prefer: str | None = None) -> dict:
    req = urllib.request.Request(f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"})
    if prefer:
        req.add_header("Prefer", prefer)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_events(token: str, start: datetime, end: datetime) -> list[dict]:
    """Return raw calendar events in the shape collectors/normalize.py expects.
    Times are requested in UTC (Prefer header); normalize converts to local."""
    s = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = (f"/me/calendarView?startDateTime={s}&endDateTime={e}"
            "&$select=subject,start,end,isAllDay,showAs,isCancelled,organizer"
            "&$orderby=start/dateTime&$top=200")
    out: list[dict] = []
    data = _graph_get(path, token, prefer='outlook.timezone="UTC"')
    for ev in data.get("value", []):
        if ev.get("isCancelled"):
            continue
        out.append({
            "subject": ev.get("subject", ""),
            "start_utc": ev["start"]["dateTime"].split(".")[0],
            "end_utc": ev["end"]["dateTime"].split(".")[0],
            "all_day": bool(ev.get("isAllDay")),
            "show_as": ev.get("showAs", "busy"),
        })
    return out


def whoami(token: str) -> dict:
    return _graph_get("/me", token)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    if cmd == "initiate":
        f = initiate()
        print(f["message"], flush=True)
    elif cmd == "poll":
        poll()
        tok = get_token()
        me = whoami(tok)
        print(f"\n✓ logged in as {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>", flush=True)
    elif cmd == "login":
        login()
        tok = get_token()
        me = whoami(tok)
        print(f"\n✓ logged in as {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}> "
              f"({me.get('jobTitle')})")
        print(f"  token cache: {_cache_path()}")
    elif cmd == "pull":
        tz_start = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=UTC)
        tz_end = datetime.fromisoformat(sys.argv[3]).replace(tzinfo=UTC)
        tok = get_token()
        evs = fetch_events(tok, tz_start, tz_end)
        print(json.dumps(evs, indent=2))
    else:
        print(__doc__)
