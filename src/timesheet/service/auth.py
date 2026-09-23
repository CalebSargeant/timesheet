"""Sign-in with GitHub — github.com or a GitHub Enterprise Server — plus the
session cookie and the policy deciding who is allowed an account at all.

An OAuth App works unchanged against an Enterprise Server: only the hostnames
move, so one `Provider` covers both and a self-hosted deployment points at its
own GHES with two settings and no code change.

Three things here are load-bearing and worth stating plainly:

  * **`state` is signed, not stored.** It carries the return path and an expiry
    in an HMAC the server can verify without a session table, which is what makes
    the callback safe against a forged sign-in on a stateless pod.
  * **The session cookie is signed, not encrypted, and holds only an account id.**
    Nothing secret is in it; the credentials it grants access to live in the
    database, encrypted separately (see `crypto.py`).
  * **Access policy is checked on every request, not only at sign-up.** Removing
    somebody from the allowlist has to lock them out of the session they already
    hold, or the control is decorative.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ..collectors.github import api_base, web_base
from ..net import InsecureUrl, open_url
from .store import GITHUB
from .users import User, defaults, user_id

log = logging.getLogger(__name__)

COOKIE = "ts_session"
STATE_TTL = 600                     # ten minutes to finish a sign-in
SESSION_TTL = 14 * 24 * 3600        # a fortnight
DOTCOM = "github.com"
# A GitHub token is renewed this long before its stated expiry, so a collector is
# never handed one with seconds left that dies halfway through a week's searches.
RENEW_MARGIN = 300

POLICIES = ("single", "allowlist", "org", "open")


class AuthError(RuntimeError):
    """Sign-in could not be completed."""


class NotAllowed(AuthError):
    """The account is real, but this deployment does not admit it."""


class Rejected(AuthError):
    """GitHub no longer accepts this token, and only a new sign-in will fix it."""


# --- configuration --------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    client_id: str = ""
    client_secret: str = ""
    host: str = DOTCOM               # github.com, or a GHES hostname
    base_url: str = ""               # this service's own public URL
    scopes: str = "read:user user:email repo read:org"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def web(self) -> str:
        """Where the browser is sent, and where the token is exchanged. Always the
        plain host, for every flavour of GitHub."""
        return web_base(self.host)

    @property
    def api(self) -> str:
        return api_base(self.host)

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/auth/callback"

    def authorize_url(self, state: str) -> str:
        q = urllib.parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "allow_signup": "false",
        })
        return f"{self.web}/login/oauth/authorize?{q}"

    @staticmethod
    def from_env(env: dict | None = None) -> Provider:
        e = env or os.environ
        return Provider(
            client_id=e.get("GITHUB_OAUTH_CLIENT_ID", ""),
            client_secret=e.get("GITHUB_OAUTH_CLIENT_SECRET", ""),
            host=e.get("GITHUB_HOST", DOTCOM),
            base_url=e.get("PUBLIC_URL", "http://localhost:8000"),
            scopes=e.get("GITHUB_OAUTH_SCOPES", "read:user user:email repo read:org"),
        )


@dataclass(frozen=True)
class Policy:
    """Who may hold an account here.

    `single` is the default on purpose. A fresh deployment of something that
    stores delegated mailbox tokens should not be an open door: the first person
    to sign in claims it, and everyone after is refused until the operator
    deliberately opens it up.
    """
    mode: str = "single"
    logins: frozenset[str] = frozenset()
    orgs: tuple[str, ...] = ()

    @staticmethod
    def from_env(env: dict | None = None) -> Policy:
        e = env or os.environ
        mode = (e.get("ACCESS_POLICY") or "single").strip().lower()
        if mode not in POLICIES:
            raise ValueError(f"ACCESS_POLICY must be one of {', '.join(POLICIES)}, not {mode!r}")
        return Policy(
            mode=mode,
            logins=frozenset(x.strip().lower()
                             for x in (e.get("ALLOWED_LOGINS") or "").split(",") if x.strip()),
            orgs=tuple(x.strip().lower()
                       for x in (e.get("ALLOWED_ORGS") or "").split(",") if x.strip()),
        )

    def describe(self) -> str:
        if self.mode == "open":
            return "anyone with a GitHub account"
        if self.mode == "single":
            return "one account — the first to sign in"
        if self.mode == "allowlist":
            return f"{len(self.logins)} allow-listed account(s)"
        return "members of " + (", ".join(self.orgs) or "no configured organisation")


def check_access(policy: Policy, login: str, uid: str, *, existing: bool,
                 user_count: int, orgs: list[str]) -> None:
    """Raise NotAllowed unless this account may sign in. Called on sign-up AND on
    every later request, so revoking access ends the session already in flight."""
    low = login.strip().lower()
    if policy.mode == "open":
        return
    if policy.mode == "allowlist":
        if low not in policy.logins:
            raise NotAllowed(f"{login} is not on this deployment's allow-list")
        return
    if policy.mode == "org":
        if not policy.orgs:
            raise NotAllowed("ACCESS_POLICY=org but ALLOWED_ORGS is empty")
        if not {o.lower() for o in orgs} & set(policy.orgs):
            raise NotAllowed(
                f"{login} is not a member of {', '.join(policy.orgs)}. Note that GitHub "
                "only reports an organisation here if your membership is public or the "
                "org has approved this OAuth app.")
        return
    # single
    if existing or user_count == 0:
        return
    raise NotAllowed(
        "this deployment is configured for a single account and one is already "
        "claimed. Set ACCESS_POLICY to open, org or allowlist to admit more.")


# --- signing --------------------------------------------------------------


def _secret() -> bytes:
    key = os.environ.get("SECRET_KEY", "")
    if not key or len(key) < 16:
        raise AuthError(
            "SECRET_KEY must be set to at least 16 characters before anyone can sign in. "
            'Generate one with `python -c "import secrets;print(secrets.token_urlsafe(48))"`.')
    return hashlib.sha256(("session:" + key).encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def _verify(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    good = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, good):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def local_path(value: str | None, default: str = "/") -> str:
    """`value` if it names a path on this host, else `default`.

    `//host` is the obvious off-site redirect. `/\\host` is the same one: browsers
    read a backslash in that position as a slash, so a check for a leading `//`
    alone lets it straight through, and `/auth/login?next=/\\evil.example` became a
    sign-in link that finished on somebody else's site. Control characters are
    refused for the same reason — nothing legitimate needs them in a path.
    """
    v = value or ""
    if (not v.startswith("/") or v.startswith("//") or "\\" in v
            or any(ord(c) < 0x20 for c in v)):
        return default
    return v


def make_state(return_to: str = "/") -> str:
    # Only a path is ever kept. Signing an absolute URL would turn the callback
    # into an open redirect that an attacker can point anywhere they like.
    path = local_path(return_to)
    return _sign({"n": secrets.token_urlsafe(12), "r": path[:200],
                  "exp": int(time.time()) + STATE_TTL})


def read_state(state: str) -> str | None:
    payload = _verify(state)
    return payload.get("r", "/") if payload else None


def make_session(uid: str) -> str:
    return _sign({"uid": uid, "iat": int(time.time()), "exp": int(time.time()) + SESSION_TTL})


def read_session(cookie: str | None) -> str | None:
    payload = _verify(cookie) if cookie else None
    return payload.get("uid") if payload else None


def csrf_token(uid: str) -> str:
    """A form token bound to the session. SameSite=Lax already blocks a
    cross-site POST in every current browser; this covers the ones it doesn't and
    any future relaxation of that default."""
    return _sign({"uid": uid, "csrf": 1, "exp": int(time.time()) + SESSION_TTL})


def check_csrf(uid: str, token: str | None) -> bool:
    payload = _verify(token) if token else None
    return bool(payload and payload.get("csrf") and payload.get("uid") == uid)


# --- the OAuth exchange ---------------------------------------------------


def _post_json(url: str, data: dict, *, timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(  # noqa: S310 — opened through net.open_url
        url, data=body, headers={"Accept": "application/json"})
    try:
        with open_url(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except InsecureUrl as e:
        raise AuthError(f"refusing to post credentials to {url[:60]!r}") from e
    except urllib.error.HTTPError as e:
        raise AuthError(f"GitHub returned HTTP {e.code} exchanging the code") from e
    except urllib.error.URLError as e:
        raise AuthError(f"cannot reach {url}: {e.reason}") from e


def _get_json(url: str, token: str, *, timeout: int = 20):
    req = urllib.request.Request(url, headers={  # noqa: S310 — opened through net.open_url, which enforces https
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with open_url(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except InsecureUrl as e:
        raise AuthError(f"refusing to send a token to {url[:60]!r}") from e
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Rejected("GitHub no longer accepts this sign-in") from e
        raise AuthError(f"GitHub returned HTTP {e.code} for {url.rsplit('/', 1)[-1]}") from e
    except urllib.error.URLError as e:
        raise AuthError(f"cannot reach {url}: {e.reason}") from e


def _token_set(got: dict, now: float) -> dict:
    """What is stored for one GitHub sign-in.

    An OAuth app registered since GitHub's August 2026 change is issued an access
    token that lives eight hours, plus a refresh token that lives six months. Only
    the access token used to be kept, so every account lost GitHub eight hours
    after signing in. Expiry is stored as a moment, since `expires_in` means nothing
    once the response that carried it is gone. A token that never expires comes
    back without a refresh token and is stored as it always was.
    """
    tokens = {"access_token": got["access_token"]}
    if got.get("refresh_token"):
        tokens["refresh_token"] = got["refresh_token"]
        if got.get("expires_in"):
            tokens["expires_at"] = int(now + int(got["expires_in"]))
        if got.get("refresh_token_expires_in"):
            tokens["refresh_expires_at"] = int(now + int(got["refresh_token_expires_in"]))
    return tokens


def exchange(provider: Provider, code: str) -> dict:
    """Authorization code -> that person's token set (see `_token_set`)."""
    got = _post_json(f"{provider.web}/login/oauth/access_token", {
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
        "code": code,
        "redirect_uri": provider.redirect_uri,
    })
    if not got.get("access_token"):
        raise AuthError(f"GitHub refused the code: {got.get('error_description') or got}")
    return _token_set(got, time.time())


def refresh(provider: Provider, refresh_token: str) -> dict:
    """A new token set for a refresh token. GitHub's refresh tokens are single use:
    the one passed in, and the access token it came with, stop working."""
    got = _post_json(f"{provider.web}/login/oauth/access_token", {
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if not got.get("access_token"):
        # A spent or expired refresh token comes back as HTTP 200 with an error
        # body (`bad_refresh_token`), never as a status code.
        raise Rejected(f"GitHub would not renew the sign-in: "
                       f"{got.get('error_description') or got.get('error') or 'no token returned'}")
    return _token_set(got, time.time())


def github_token(store, provider: Provider, uid: str) -> str:
    """This account's GitHub access token, renewed first if it is about to expire.

    The web process and the scheduled job both call this, and a refresh token is
    single use, so the two can race to renew the same one. The loser is refused,
    but by then the winner has stored the new pair, so a refusal is checked
    against the store before it is believed.

    A token that cannot be renewed is returned anyway. GitHub's 401 on it is what
    tells the person, on their own timesheet, to sign in again; returning nothing
    would drop their GitHub activity without a word.
    """
    cred = store.get_credential(uid, GITHUB) or {}
    token = cred.get("access_token", "")
    renew_with = cred.get("refresh_token")
    expires_at = cred.get("expires_at")
    if not renew_with or not expires_at or expires_at - RENEW_MARGIN > time.time():
        return token
    try:
        fresh = refresh(provider, renew_with)
    except AuthError as e:
        latest = store.get_credential(uid, GITHUB) or {}
        if latest.get("refresh_token") != renew_with:
            return latest.get("access_token", "")
        log.warning("could not renew the GitHub sign-in for %s: %s", uid, e)
        return token
    try:
        account = (store.credential_meta(uid, GITHUB) or {}).get("account", "")
        store.put_credential(uid, GITHUB, fresh, account=account)
    # The store's own failure, whatever it is. The new token in hand still works for
    # this run; what is lost is the next renewal, as the old refresh token is spent.
    except Exception:
        log.exception("could not store the renewed GitHub sign-in for %s", uid)
    return fresh["access_token"]


def github_problem(provider: Provider, token: str) -> str:
    """Why this account's GitHub token cannot be used, or "" if GitHub accepts it.

    Asked of GitHub rather than worked out from the stored expiry: a token can also
    be revoked, and a status that is green by arithmetic is how a dead connection
    showed green on the Connections page for a week.
    """
    if not token:
        return "no GitHub token is stored for this account"
    try:
        _get_json(f"{provider.api}/user", token)
    except AuthError as e:
        return str(e)
    return ""


def identify(provider: Provider, token: str) -> tuple[dict, str, list[str]]:
    """(profile, primary email, org logins) for the holder of `token`.

    Email and org membership are both best-effort. A user can hide their email,
    and GitHub only lists an organisation here when the membership is public or
    the org has approved the OAuth app — so a missing org is not proof of
    non-membership, only of what this token is allowed to see. `check_access`
    says as much in its error when an org policy rejects somebody.
    """
    profile = _get_json(f"{provider.api}/user", token)

    email = profile.get("email") or ""
    if not email:
        try:
            addresses = _get_json(f"{provider.api}/user/emails", token)
            primary = next((a for a in addresses if a.get("primary") and a.get("verified")), None)
            email = (primary or {}).get("email", "") if primary else ""
        except AuthError:
            log.debug("could not read the user's email addresses", exc_info=True)

    orgs: list[str] = []
    try:
        orgs = [o.get("login", "") for o in _get_json(f"{provider.api}/user/orgs", token)]
    except AuthError:
        log.debug("could not read the user's organisations", exc_info=True)

    return profile, email, [o for o in orgs if o]


def upsert(store, provider: Provider, policy: Policy, profile: dict, email: str,
           orgs: list[str], *, tz: str = "UTC") -> User:
    """Create or refresh the account for a completed sign-in, policy permitting."""
    account_id = profile.get("id")
    login = profile.get("login") or ""
    if not account_id or not login:
        raise AuthError("GitHub did not return an account id")

    host = provider.host.strip().lower().removeprefix("https://").rstrip("/") or DOTCOM
    uid = user_id(host, account_id)
    existing = store.get_user(uid)
    check_access(policy, login, uid, existing=existing is not None,
                 user_count=store.count_users(), orgs=orgs)

    now = datetime.now(ZoneInfo(tz))
    settings = dict(existing.settings) if existing else defaults()
    # Seed the GitHub identity from the sign-in, so a new account can pull its own
    # commits before anyone opens the settings page. `setdefault` is wrong here:
    # the defaults already carry github.com, so on an Enterprise Server deployment
    # every new account would be pointed at the wrong host and find no commits at
    # all. A host the user has since changed themselves is left alone.
    if existing is None:
        settings["github_host"] = host
    if not settings.get("github_user"):
        settings["github_user"] = login

    user = User(
        id=uid, login=login, host=host,
        name=profile.get("name") or login,
        email=email or (existing.email if existing else ""),
        avatar_url=profile.get("avatar_url") or "",
        # The account that claims an empty deployment administers it. Later
        # accounts never gain the flag by signing in.
        is_admin=existing.is_admin if existing else store.count_users() == 0,
        created_at=existing.created_at if existing else now,
        last_seen=now,
        settings=settings,
    )
    return store.save_user(user)


def cookie_header(value: str, *, secure: bool, max_age: int = SESSION_TTL) -> str:
    flags = ["Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
    if secure:
        flags.append("Secure")
    return f"{COOKIE}={value}; " + "; ".join(flags)


def clear_cookie(*, secure: bool) -> str:
    return cookie_header("", secure=secure, max_age=0)
