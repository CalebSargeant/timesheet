"""Transport for the Microsoft 365 MCP connector. No model, no agent.

Why this exists at all: many tenants block the device-code Microsoft Graph path
(`AADSTS65002` / `700016`) and grant ordinary staff no app registrations, which
leaves a published-calendar ICS link — subjects hidden, a rolling three-month
window, and a world-readable secret URL — as the only way in. The connector at
`https://microsoft365.mcp.claude.com/mcp` is a plain streamable-HTTP MCP server
that validates an Entra token issued for an app pair Anthropic registered
multi-tenant. Where a tenant has already consented to that pair, a device-code
sign-in against it is not blocked, needs no admin action, and `offline_access`
makes every later run headless.

Nothing here escalates: every scope is delegated, so the reach is capped at what
the signed-in person can already open in Outlook and Teams themselves.

**One session per person.** `McpSession` holds its own credentials and its own
handshake state, so a multi-user deployment can hold many at once with no chance
of one account's token answering another's request. The module-level helpers
wrap a single session backed by a local cache file, which is what the CLI and a
single-user install use.

The sign-in is split into `start_device_code()` and `poll_device_code()` rather
than one blocking call, so a web UI can show the code and poll from the browser
instead of holding a request open for the three minutes a human takes.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..net import InsecureUrl, open_url

log = logging.getLogger(__name__)

CLIENT_ID = os.environ.get("M365_MCP_CLIENT_ID", "08ad6f98-a4f8-4635-bb8d-f1a3044760f0")
SCOPE = os.environ.get(
    "M365_MCP_SCOPE",
    "api://07c030f6-5743-41b7-ba00-0a6e85f37c17/access_as_user offline_access",
)
MCP_URL = os.environ.get("M365_MCP_URL", "https://microsoft365.mcp.claude.com/mcp")
# 'organizations' lets Entra resolve the signed-in user's home tenant. Pin it
# only if sign-in lands in the wrong directory — a UPN can look identical in two,
# and the wrong one gives entirely convincing wrong answers.
TENANT = os.environ.get("M365_MCP_TENANT", "organizations")
PROTOCOL = "2025-06-18"

# A tenant is one path segment: a GUID, a verified domain, or an Entra alias.
# Interpolating anything else into the authority URL would let a stray setting
# steer the token request somewhere else entirely.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CACHE = Path(os.path.expanduser(os.environ.get("M365_MCP_TOKEN_CACHE", "~/.m365-mcp-token.json")))
SEED = os.environ.get("M365_MCP_TOKEN_SEED", "")
SEED_JSON = os.environ.get("M365_MCP_TOKEN_JSON", "")

# The connector caps a page at 25 whatever you ask for, so a short page is not
# evidence of the end of the results.
PAGE_LIMIT = 25
MAX_PAGES = 40


class McpError(RuntimeError):
    """The connector could not be reached, or answered with an error."""


class McpAuthError(McpError):
    """No usable token, and this process may not stop to ask for one."""


def authority(tenant: str | None = None) -> str:
    t = (tenant or TENANT).strip()
    if not _TENANT_RE.match(t):
        raise ValueError(f"M365 tenant is not a single URL path segment: {t!r}")
    return f"https://login.microsoftonline.com/{t}/oauth2/v2.0"


# --- http ------------------------------------------------------------------


def _urlopen(url: str, *, data: bytes, headers: dict | None = None, timeout: int):
    """Open an **https** URL, and nothing else.

    Both URLs this module opens are assembled from settings (the connector URL,
    the tenant), so the scheme check in `net` is what stops a mistyped or hostile
    value turning a token request into a local file read. Re-raised as McpError
    so callers have one exception type to catch.
    """
    try:
        return open_url(url, data=data, headers=headers, timeout=timeout)
    except InsecureUrl as e:
        raise McpError(str(e)) from e


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


def _expiry(access_token: str) -> float:
    """`exp` out of the JWT without validating it — Entra already did."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return 0.0


# --- device code, split so a browser can drive it --------------------------


@dataclass(frozen=True)
class DeviceCode:
    """A sign-in in progress. `user_code` and `verification_uri` go to the human;
    `device_code` is the secret the poller presents and must not be shown."""
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def public(self) -> dict:
        """What is safe to render in a page: never the device_code itself."""
        return {"user_code": self.user_code, "verification_uri": self.verification_uri,
                "expires_in": max(0, int(self.expires_at - time.time())),
                "interval": self.interval}


def start_device_code(*, tenant: str | None = None) -> DeviceCode:
    started = _post_form(f"{authority(tenant)}/devicecode",
                         {"client_id": CLIENT_ID, "scope": SCOPE})
    if "user_code" not in started:
        raise McpAuthError(
            f"device code request failed: {started.get('error')}: "
            f"{started.get('error_description', '')[:300]}")
    return DeviceCode(
        device_code=started["device_code"],
        user_code=started["user_code"],
        verification_uri=started["verification_uri"],
        expires_at=time.time() + int(started.get("expires_in", 900)),
        interval=int(started.get("interval", 5)),
    )


def poll_device_code(pending: DeviceCode, *, tenant: str | None = None) -> dict | None:
    """The token set once the human has signed in, or None while they haven't.

    `None` means "still waiting" and nothing else; anything genuinely wrong
    raises. A caller that treats a failure as "keep polling" would spin forever
    on a declined sign-in.
    """
    if pending.expired:
        raise McpAuthError("the sign-in code expired before it was used")
    got = _post_form(f"{authority(tenant)}/token", {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": CLIENT_ID,
        "device_code": pending.device_code,
    })
    if "access_token" in got:
        return got
    error = got.get("error")
    if error in ("authorization_pending", "slow_down"):
        return None
    raise McpAuthError(
        f"sign-in failed: {error}: {got.get('error_description', '')[:300]}")


# --- a session ------------------------------------------------------------


@dataclass
class McpSession:
    """One person's connection to the connector.

    `tokens` is the Entra token set; `on_rotate` is called with the replacement
    every time Entra hands one back, which it does on *every* refresh. Dropping
    that callback silently replays a spent refresh token until it dies — roughly
    90 days later, long after the change that caused it.
    """
    tokens: dict = field(default_factory=dict)
    tenant: str | None = None
    on_rotate: Callable[[dict], None] | None = None
    url: str = MCP_URL
    _ready: bool = field(default=False, repr=False)

    @property
    def connected(self) -> bool:
        return bool(self.tokens.get("refresh_token") or self.tokens.get("access_token"))

    def _rotate(self, fresh: dict) -> None:
        self.tokens = fresh
        if self.on_rotate:
            try:
                self.on_rotate(fresh)
            # The caller's persistence, so it can fail in any way at all. The
            # token in hand still works; what is lost is next time's refresh.
            except Exception:
                log.warning("m365-mcp: could not persist the rotated token", exc_info=True)

    def access_token(self) -> str:
        """A live access token, refreshed silently. Never starts a device flow:
        an unattended refresh has no human to type a code."""
        current = self.tokens.get("access_token")
        if current and _expiry(current) - 120 > time.time():
            return current
        refresh = self.tokens.get("refresh_token")
        if refresh:
            fresh = _post_form(f"{authority(self.tenant)}/token", {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh,
                "scope": SCOPE,
            })
            if "access_token" in fresh:
                self._rotate(fresh)
                return fresh["access_token"]
            log.warning("m365-mcp: refresh rejected (%s: %s)",
                        fresh.get("error"), fresh.get("error_description", "")[:200])
        raise McpAuthError(
            "no usable Microsoft 365 token — sign in again to reconnect the account")

    # JSON-RPC ids must be unique within a session. The connector is stateless and
    # answers one request per POST, so reusing 1 happens to work — but a server
    # that starts rejecting duplicates would fail on the second call with an error
    # naming neither the id nor this module.
    _ids = itertools.count(1)

    def rpc(self, method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": next(McpSession._ids),
                           "method": method, "params": params}).encode()
        try:
            r = _urlopen(self.url, data=body, timeout=120, headers={
                "Authorization": "Bearer " + self.access_token(),
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
            raise McpError(
                f"MCP error {answer['error'].get('code')}: {answer['error'].get('message')}")
        return answer["result"]

    def handshake(self) -> None:
        if self._ready:
            return
        self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                "clientInfo": {"name": "timesheet", "version": "1"}})
        self._ready = True

    def call(self, tool: str, **arguments) -> Page:
        """Call one tool and split its content blocks.

        A block that cannot be placed is recorded as a note, which marks the
        search incomplete. Neither extreme is right: skipping it silently would
        make a moved payload look like a quiet week, and raising would let one
        new metadata block — at an endpoint this project does not own and cannot
        version — break every calendar fetch outright.
        """
        self.handshake()
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
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
                log.warning("m365-mcp: %s returned an unrecognised block %s",
                            tool, sorted(parsed))
                page.notes.append(f"unrecognised content block: {sorted(parsed)}")
        return page

    def search(self, tool: str, *, max_pages: int = MAX_PAGES, **arguments) -> SearchResult:
        """Page a search to the end. `notes`/`truncated` say when it is partial."""
        items: list[dict] = []
        notes: list[str] = []
        total: int | None = None
        offset = 0
        truncated = False

        for _ in range(max_pages):
            page = self.call(tool, limit=PAGE_LIMIT, offset=offset, **arguments)
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
            log.warning("m365-mcp: %s gave more than %d pages; the answer is partial",
                        tool, max_pages)

        for note in notes:
            log.warning("m365-mcp: %s said: %s", tool, note[:200])
        return SearchResult(items=items, notes=notes, total=total, truncated=truncated)

    def tools(self) -> list[dict]:
        self.handshake()
        return self.rpc("tools/list", {})["tools"]

    def me(self) -> dict:
        """The signed-in user, or {} if the connector answered with nothing."""
        items = self.call("get_me").items
        return items[0] if items else {}


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


@dataclass(frozen=True)
class SearchResult:
    items: list[dict]
    notes: list[str]
    total: int | None
    truncated: bool

    @property
    def complete(self) -> bool:
        return not self.truncated and not self.notes


# --- the file-backed default session (CLI, single-user install) ------------


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


def _save_to_cache(payload: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
    except OSError as e:
        log.warning("m365-mcp: cannot write %s (%s); the rotated refresh token is lost", path, e)


_default: McpSession | None = None


def default_session() -> McpSession:
    """The process-wide session backed by the local cache file."""
    global _default
    if _default is None:
        path = _cache_path()
        tokens: dict = {}
        if path.exists():
            try:
                tokens = json.loads(path.read_text())
            except (OSError, ValueError) as e:
                log.warning("m365-mcp: the cache at %s is unreadable (%s)", path, e)
        _default = McpSession(tokens=tokens, on_rotate=_save_to_cache)
    return _default


def call(tool: str, **arguments) -> Page:
    return default_session().call(tool, **arguments)


def search(tool: str, *, max_pages: int = MAX_PAGES, **arguments) -> SearchResult:
    return default_session().search(tool, max_pages=max_pages, **arguments)


def tools() -> list[dict]:
    return default_session().tools()


def login(*, tenant: str | None = None) -> dict:
    """Interactive device-code sign-in for the CLI. Blocks until the human is done."""
    pending = start_device_code(tenant=tenant)
    print(f"\nOpen {pending.verification_uri} and enter code: {pending.user_code}\n",
          file=sys.stderr, flush=True)
    interval = pending.interval
    while not pending.expired:
        got = poll_device_code(pending, tenant=tenant)
        if got:
            _save_to_cache(got)
            default_session().tokens = got
            return got
        interval += 0 if interval > 10 else 1
        time.sleep(interval)
    raise McpAuthError("device code expired before the sign-in completed")


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
        login()
        me = default_session().me()
        print(f"signed in as {me.get('displayName')} ({me.get('jobTitle')})")
        print(f"token cache: {_cache_path()}  — this file is a credential")
        return 0
    if cmd == "tools":
        for t in tools():
            props = ", ".join(t.get("inputSchema", {}).get("properties", {}))
            print(f"{t['name']}\n    params: {props}")
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
