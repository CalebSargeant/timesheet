"""Transport for Claude's Microsoft 365 MCP connector. No model, no agent.

Why this exists: the LOCGOV tenant blocks the device-code Graph path
(`AADSTS65002` / `700016`) and grants no app registrations, which is what pushed
this project onto a published ICS link in the first place. The connector at
`https://microsoft365.mcp.claude.com/mcp` is a plain streamable-HTTP MCP server
that validates an Entra token issued for an app pair Anthropic registered
multi-tenant and the tenant has already consented to. A device-code sign-in
against *that* pair is not blocked, needs no admin action, and `offline_access`
makes every later run headless.

What it buys over the ICS link: real event subjects (a published calendar set to
"availability only" hides them all behind a bare "Busy", which is why
`leave_blank_subjects` exists), no rolling three-month truncation, no
world-readable secret URL, and -- the part ICS cannot do at all -- email and
Teams activity, which is what `/api/ingest` and the Power Automate flow were
built to push in.

The two GUIDs below are Anthropic's public multi-tenant registrations, not the
tenant's. Nothing here escalates: every scope is delegated, so the reach is
capped at what the signed-in user can already open in Outlook and Teams.

Env is namespaced `M365_MCP_*` on purpose. `m365_graph.py` uses `M365_CLIENT_ID`
and `M365_TOKEN_CACHE` for a *different* client and an MSAL cache in a
*different* format; sharing either name would have one collector quietly reading
the other's file.

    python -m timesheet.collectors.mcp_client login     # once, interactive
    python -m timesheet.collectors.mcp_client tools
    python -m timesheet.collectors.mcp_client call get_me
"""
from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CLIENT_ID = os.environ.get("M365_MCP_CLIENT_ID", "08ad6f98-a4f8-4635-bb8d-f1a3044760f0")
SCOPE = os.environ.get(
    "M365_MCP_SCOPE",
    "api://07c030f6-5743-41b7-ba00-0a6e85f37c17/access_as_user offline_access",
)
MCP_URL = os.environ.get("M365_MCP_URL", "https://microsoft365.mcp.claude.com/mcp")
# 'organizations' lets Entra resolve the signed-in user's home tenant. Pin it
# only if sign-in lands in the wrong directory — the UPN is identical in both of
# ours, so the wrong one looks entirely convincing while giving wrong answers.
TENANT = os.environ.get("M365_MCP_TENANT", "organizations")
PROTOCOL = "2025-06-18"

# A tenant is one path segment: a GUID, a verified domain, or an Entra alias.
# Interpolating anything else into the authority URL would let an env var steer
# the token request somewhere else entirely.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CACHE = Path(os.path.expanduser(os.environ.get("M365_MCP_TOKEN_CACHE", "~/.m365-mcp-token.json")))
SEED = os.environ.get("M365_MCP_TOKEN_SEED", "")
SEED_JSON = os.environ.get("M365_MCP_TOKEN_JSON", "")

if not _TENANT_RE.match(TENANT):
    raise ValueError(f"M365_MCP_TENANT is not a single URL path segment: {TENANT!r}")

_AUTH = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"

# The connector caps a page at 25 whatever you ask for, so a short page is not
# evidence of the end of the results.
PAGE_LIMIT = 25
MAX_PAGES = 40


class McpError(RuntimeError):
    """The connector could not be reached, or answered with an error."""


class McpAuthError(McpError):
    """No usable token, and this process may not stop to ask for one."""


# --- auth ------------------------------------------------------------------


def _urlopen(url: str, *, data: bytes, headers: dict | None = None, timeout: int):
    """Open an **https** URL, and nothing else.

    Both URLs this module opens are assembled from env vars (M365_MCP_URL,
    M365_MCP_TENANT) and urllib also speaks file:// and ftp://. Without this
    check a mistyped or hostile variable turns a token request into a local file
    read whose contents are then posted onward.
    """
    if not url.startswith("https://"):
        raise McpError(f"refusing to open a non-https URL: {url[:60]!r}")
    req = urllib.request.Request(url, data=data, headers=headers or {})
    # nosec B310 - the scheme is checked immediately above
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  # nosemgrep


def _post_form(url: str, data: dict) -> dict:
    try:
        with _urlopen(url, data=urllib.parse.urlencode(data).encode(), timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Entra puts the reason in the body of a 4xx, so the body is the point.
        try:
            return json.loads(e.read().decode())
        except (ValueError, OSError) as inner:
            raise McpError(f"{url} -> HTTP {e.code}") from inner
    except urllib.error.URLError as e:
        raise McpError(f"cannot reach {url}: {e.reason}") from e


def _cache_path() -> Path:
    """The writable cache, seeded once from a read-only secret if one is given.

    Entra rotates the refresh token on every use and a mounted Secret is
    read-only, so the rotation has to land somewhere else. Losing it when the
    pod exits is fine — the seed keeps its own 90-day sliding window — but the
    token has to be *used* inside that window or it dies.
    """
    if CACHE.exists():
        return CACHE
    if SEED and Path(SEED).exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SEED, CACHE)
        CACHE.chmod(0o600)
        # The path, never the contents. Nothing here logs a token,
        # an authorisation header or a device code.
        log.info("m365-mcp: cache seeded from %s", SEED)
    elif SEED_JSON.strip():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(SEED_JSON)
        CACHE.chmod(0o600)
        log.info("m365-mcp: cache seeded from the environment")
    return CACHE


def _save(payload: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
    except OSError as e:
        log.warning("m365-mcp: cannot write %s (%s); the rotated refresh token is lost", path, e)


def _expiry(access_token: str) -> float:
    """`exp` out of the JWT without validating it — Entra already did."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return 0.0


def _device_code() -> dict:
    started = _post_form(f"{_AUTH}/devicecode", {"client_id": CLIENT_ID, "scope": SCOPE})
    if "user_code" not in started:
        raise McpAuthError(
            f"device code request failed: {started.get('error')}: "
            f"{started.get('error_description', '')[:300]}"
        )
    print(f"\nOpen {started['verification_uri']} and enter code: {started['user_code']}\n",
          file=sys.stderr, flush=True)
    deadline = time.time() + int(started["expires_in"])
    interval = int(started.get("interval", 5))
    while time.time() < deadline:
        got = _post_form(f"{_AUTH}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT_ID,
            "device_code": started["device_code"],
        })
        if "access_token" in got:
            return got
        if got.get("error") == "slow_down":
            interval += 5
        elif got.get("error") != "authorization_pending":
            raise McpAuthError(
                f"sign-in failed: {got.get('error')}: {got.get('error_description', '')[:300]}")
        time.sleep(interval)
    raise McpAuthError("device code expired before the sign-in completed")


def token(*, allow_device_code: bool = False) -> str:
    """A live access token, refreshed silently from the cache.

    `allow_device_code` is False by default because a device flow blocks until a
    human types a code into a browser, and the nightly CronJob has no human.
    """
    path = _cache_path()
    cached: dict | None = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("m365-mcp: token cache at %s is unreadable (%s)", path, e)

    if cached and "access_token" in cached and _expiry(cached["access_token"]) - 120 > time.time():
        return cached["access_token"]

    if cached and cached.get("refresh_token"):
        fresh = _post_form(f"{_AUTH}/token", {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": cached["refresh_token"],
            "scope": SCOPE,
        })
        if "access_token" in fresh:
            _save(fresh)
            return fresh["access_token"]
        log.warning("m365-mcp: refresh rejected (%s: %s)",
                    fresh.get("error"), fresh.get("error_description", "")[:200])

    if not allow_device_code:
        raise McpAuthError(
            f"no usable M365 MCP token at {path}. Run "
            "`python -m timesheet.collectors.mcp_client login` once and put the resulting "
            "JSON in the vault as M365_MCP_TOKEN_JSON; a cron run will not stop to sign in.")
    issued = _device_code()
    _save(issued)
    return issued["access_token"]


# --- transport -------------------------------------------------------------

_ready = False


# JSON-RPC ids must be unique within a session. The connector is stateless and
# answers one request per POST, so reusing 1 happens to work — but a server that
# starts rejecting duplicates would fail on the second call with an error naming
# neither the id nor this module.
_request_id = itertools.count(1)


def _rpc(method: str, params: dict) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": next(_request_id), "method": method, "params": params}).encode()
    try:
        r = _urlopen(MCP_URL, data=body, timeout=120, headers={
            "Authorization": "Bearer " + token(),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        })
        with r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        raise McpError(f"MCP HTTP {e.code}: {e.read().decode()[:300]}") from e
    except urllib.error.URLError as e:
        raise McpError(f"cannot reach the MCP connector: {e.reason}") from e

    for line in raw.splitlines():          # the server may answer as SSE
        if line.startswith("data: "):
            raw = line[6:]
            break
    try:
        answer = json.loads(raw)
    except ValueError as e:
        raise McpError(f"MCP returned a non-JSON body: {raw[:200]!r}") from e
    if "error" in answer:
        raise McpError(f"MCP error {answer['error'].get('code')}: {answer['error'].get('message')}")
    return answer["result"]


def _handshake() -> None:
    global _ready
    if _ready:
        return
    _rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                        "clientInfo": {"name": "github-timesheet", "version": "1"}})
    _ready = True


@dataclass
class Page:
    """One page, taken apart into its block kinds.

    The connector answers with a LIST of content blocks, one JSON object each:
    an optional `searchInfo` header, one per result, a pagination footer, and —
    when a scan was cut short — a block of plain prose. `''.join(...)`, the
    obvious client, yields `}{`-concatenated JSON and eats the prose note that
    was the only statement that the answer is partial.
    """
    items: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    next_offset: int | None = None
    total: int | None = None


# A single-page answer ends `{"totalResultCount": 12}` with no nextOffset at
# all; the chat search's footer carries no total. Any one of these marks it.
_FOOTER_KEYS = {"nextOffset", "moreResults", "totalResultCount", "nextCursor"}


def call(tool: str, **arguments) -> Page:
    """Call one tool and split its content blocks.

    A block that cannot be placed is recorded as a note, which marks the search
    incomplete. Neither extreme is right: skipping it silently would make a
    moved payload look like a quiet week, and raising would let one new metadata
    block — at an endpoint Anthropic owns and does not document — break every
    calendar fetch outright.
    """
    _handshake()
    result = _rpc("tools/call", {"name": tool, "arguments": arguments})
    blocks = result.get("content", [])
    if result.get("isError"):
        raise McpError(f"{tool}: {' '.join(b.get('text', '') for b in blocks)[:400]}")

    page = Page()
    for block in blocks:
        text = block.get("text", "")
        if not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            page.notes.append(text.strip())
            continue
        if not isinstance(parsed, dict):
            raise McpError(f"{tool}: unexpected content block {text[:120]!r}")
        if "uri" in parsed or "id" in parsed:
            page.items.append(parsed)
        elif "searchInfo" in parsed:
            page.info = parsed["searchInfo"]
        elif _FOOTER_KEYS & parsed.keys():
            page.next_offset = parsed.get("nextOffset")
            page.total = parsed.get("totalResultCount")
        else:
            log.warning("m365-mcp: %s returned an unrecognised block %s", tool, sorted(parsed))
            page.notes.append(f"unrecognised content block: {sorted(parsed)}")
    return page


@dataclass(frozen=True)
class SearchResult:
    items: list[dict]
    notes: list[str]
    total: int | None
    truncated: bool

    @property
    def complete(self) -> bool:
        return not self.truncated and not self.notes


def search(tool: str, *, max_pages: int = MAX_PAGES, **arguments) -> SearchResult:
    """Page a search to the end. `notes`/`truncated` say when the answer is partial."""
    items: list[dict] = []
    notes: list[str] = []
    total: int | None = None
    offset = 0
    truncated = False

    for _ in range(max_pages):
        page = call(tool, limit=PAGE_LIMIT, offset=offset, **arguments)
        items.extend(page.items)
        notes.extend(page.notes)
        if page.total is not None:
            total = page.total
        if page.next_offset is None:
            break
        if page.next_offset <= offset:
            raise McpError(f"{tool}: pagination did not advance past offset {offset}")
        offset = page.next_offset
    else:
        truncated = True
        log.warning("m365-mcp: %s gave more than %d pages; the answer is partial", tool, max_pages)

    for note in notes:
        log.warning("m365-mcp: %s said: %s", tool, note[:200])
    return SearchResult(items=items, notes=notes, total=total, truncated=truncated)


def tools() -> list[dict]:
    _handshake()
    return _rpc("tools/list", {})["tools"]


def _coerce(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        return v


def main(argv: list[str]) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "login":
        token(allow_device_code=True)
        me = call("get_me").items[0]
        print(f"signed in as {me.get('displayName')} ({me.get('jobTitle')})")
        print(f"token cache: {_cache_path()}  — this file is a credential")
        return 0
    if cmd == "tools":
        for t in tools():
            print(f"{t['name']}\n    params: {', '.join(t.get('inputSchema', {}).get('properties', {}))}")
        return 0
    if cmd == "call" and len(argv) > 2:
        kw = {k: _coerce(v) for k, v in (a.split("=", 1) for a in argv[3:])}
        for item in call(argv[2], **kw).items:
            print(json.dumps(item))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
