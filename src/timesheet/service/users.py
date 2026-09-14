"""The account model: who is signed in, what they configured, what they connected.

Everything here is per person. A `User` is identified by the GitHub host plus the
numeric account id — not the login — because a login can be renamed or, on
github.com, released and taken by somebody else, and either would silently hand
one person's mailbox tokens to another.

`SETTINGS` is the whole of what a user may change about their own reconstruction.
It is an explicit allow-list with a type and a bound on every entry, because these
values are submitted from a browser and then fed straight into `Config`: without
it, a posted `min_day_minutes=999999` is a 16-hour day and a posted `tz=../../etc`
is a crash on every page load.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import i18n

log = logging.getLogger(__name__)

DELIVERY_CHANNELS = ("none", "email", "chat")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Field:
    """One editable setting: how to read it from a form, and what is allowed."""
    name: str
    kind: str                       # str | int | bool | time | tz | locale | email | choice | days
    default: Any = None
    low: int = 0
    high: int = 0
    choices: tuple[str, ...] = ()
    max_len: int = 200
    # The environment variable a deployment sets to change the STARTING value for
    # new accounts. None means the field is per-person and a deployment-wide
    # default would be wrong — an identity, a manager's address, whether that
    # person wants anything sent at all.
    env: str | None = None


SETTINGS: tuple[Field, ...] = (
    # Presentation
    Field("locale", "locale", i18n.DEFAULT_LOCALE, env="LOCALE"),
    Field("tz", "tz", "UTC", env="TZ"),

    # The shape of a working day
    Field("day_start", "time", "08:30", env="DAY_START"),
    Field("earliest_start_floor", "time", "06:00", env="EARLIEST_START_FLOOR"),
    Field("min_day_minutes", "int", 480, low=0, high=16 * 60, env="MIN_DAY_MINUTES"),
    Field("admin_floor_minutes", "int", 30, low=0, high=8 * 60, env="ADMIN_FLOOR_MINUTES"),
    Field("day_rollover_hour", "int", 5, low=0, high=12, env="DAY_ROLLOVER_HOUR"),
    Field("workdays", "days", (0, 1, 2, 3, 4)),
    Field("rota_enabled", "bool", False, env="ROTA_ENABLED"),
    Field("rota_minutes", "int", 75, low=0, high=8 * 60, env="ROTA_MINUTES"),

    # Which signals count
    Field("include_reviews", "bool", True, env="INCLUDE_REVIEWS"),
    Field("include_authored", "bool", True, env="INCLUDE_AUTHORED"),
    Field("include_email", "bool", True, env="INCLUDE_EMAIL"),
    Field("include_chat", "bool", True, env="INCLUDE_CHAT"),

    # Where the work is
    Field("github_host", "str", "github.com", max_len=120),
    Field("github_user", "str", "", max_len=100),

    # Who receives it
    Field("manager_name", "str", "", max_len=120),
    Field("manager_email", "email", ""),
    Field("manager_chat", "email", ""),
    Field("delivery_channel", "choice", "none", choices=DELIVERY_CHANNELS),
    Field("delivery_enabled", "bool", False),
)

SETTINGS_BY_NAME = {f.name: f for f in SETTINGS}

# Settings that belong to the account rather than to the reconstruction, and so
# must never be handed to `Config.with_settings`.
_ACCOUNT_ONLY = frozenset({
    "github_host", "github_user", "manager_name", "manager_email", "manager_chat",
    "delivery_channel", "delivery_enabled",
})


def defaults(env: dict | None = None) -> dict:
    """The starting values a new account gets.

    A deployment declares these in the environment (the chart's `config` block),
    and before this read them a `TZ` or `LOCALE` set there was silently ignored
    for every account created — so the chart's own "starting values for a new
    account" comment was untrue.

    Each value goes through `coerce`, so a typo in a deployment's environment is
    logged and falls back to the built-in default rather than poisoning every
    account that is ever created. Per-person fields (an identity, a manager, a
    delivery switch) carry no `env` and are never pre-set.
    """
    e = env if env is not None else os.environ
    out: dict[str, Any] = {}
    for f in SETTINGS:
        value = f.default
        if f.env and e.get(f.env) not in (None, ""):
            try:
                value = coerce(f.name, e[f.env])
            except ValueError as exc:
                log.warning("ignoring %s=%r from the environment: %s", f.env, e[f.env], exc)
        out[f.name] = value
    return out


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def coerce(name: str, value: Any) -> Any:
    """One submitted value -> a safe stored value. Raises ValueError if it isn't."""
    spec = SETTINGS_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unknown setting {name!r}")

    if spec.kind == "bool":
        return _as_bool(value)

    if spec.kind == "int":
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError) as e:
            raise ValueError(f"{name} must be a whole number") from e
        if not (spec.low <= n <= spec.high):
            raise ValueError(f"{name} must be between {spec.low} and {spec.high}")
        return n

    if spec.kind == "days":
        if isinstance(value, str):
            parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
        else:
            parts = list(value or [])
        out = sorted({int(p) for p in parts})
        if any(d < 0 or d > 6 for d in out):
            raise ValueError("workdays must be numbers 0 (Monday) to 6 (Sunday)")
        return tuple(out)

    text = str(value or "").strip()

    if spec.kind == "time":
        if not _HHMM.match(text):
            raise ValueError(f"{name} must look like 08:30")
        return text

    if spec.kind == "tz":
        if not text:
            return spec.default
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
            raise ValueError(f"{text!r} is not a known time zone") from e
        return text

    if spec.kind == "locale":
        return i18n.get(text).code

    if spec.kind == "choice":
        if text not in spec.choices:
            raise ValueError(f"{name} must be one of {', '.join(spec.choices)}")
        return text

    if spec.kind == "email":
        if not text:
            return ""
        if not _EMAIL.match(text) or len(text) > spec.max_len:
            raise ValueError(f"{name} must be an email address")
        return text

    if len(text) > spec.max_len:
        raise ValueError(f"{name} is too long (max {spec.max_len})")
    return text


def clean(submitted: dict) -> tuple[dict, list[str]]:
    """Coerce a whole form. Returns (values, errors); a bad field is dropped, not
    fatal, so one mistyped time zone does not discard the rest of the page."""
    out, errors = {}, []
    for name, raw in (submitted or {}).items():
        if name not in SETTINGS_BY_NAME:
            continue
        try:
            out[name] = coerce(name, raw)
        except ValueError as e:
            errors.append(str(e))
    # An unchecked checkbox is absent from a form post rather than false, so any
    # boolean the form could have carried has to be read as false when missing.
    return out, errors


def for_config(settings: dict) -> dict:
    """The subset of a user's settings that `Config` understands."""
    return {k: v for k, v in (settings or {}).items()
            if k in SETTINGS_BY_NAME and k not in _ACCOUNT_ONLY}


@dataclass
class Connection:
    """One linked external account."""
    provider: str                  # github | microsoft
    account: str = ""              # what to show the user: a login, a UPN
    connected_at: datetime | None = None
    last_used: datetime | None = None
    error: str = ""                # the last failure, so the UI can say "reconnect"

    @property
    def ok(self) -> bool:
        return bool(self.account) and not self.error


@dataclass
class User:
    id: str                        # 'github.com:12345' — host + immutable account id
    login: str
    host: str = "github.com"
    name: str = ""
    email: str = ""
    avatar_url: str = ""
    is_admin: bool = False
    created_at: datetime | None = None
    last_seen: datetime | None = None
    settings: dict = field(default_factory=defaults)

    @property
    def display(self) -> str:
        return self.name or self.login

    @property
    def locale(self) -> str:
        return i18n.get(self.settings.get("locale")).code

    @property
    def strings(self) -> i18n.Locale:
        return i18n.get(self.settings.get("locale"))

    def setting(self, name: str) -> Any:
        return self.settings.get(name, SETTINGS_BY_NAME[name].default)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "login": self.login, "host": self.host, "name": self.name,
            "email": self.email, "avatar_url": self.avatar_url, "is_admin": self.is_admin,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "settings": self.settings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> User:
        def _dt(v):
            return datetime.fromisoformat(v) if v else None
        merged = defaults()
        merged.update(d.get("settings") or {})
        if isinstance(merged.get("workdays"), list):
            merged["workdays"] = tuple(merged["workdays"])
        return cls(
            id=d["id"], login=d.get("login", ""), host=d.get("host", "github.com"),
            name=d.get("name", ""), email=d.get("email", ""),
            avatar_url=d.get("avatar_url", ""), is_admin=bool(d.get("is_admin")),
            created_at=_dt(d.get("created_at")), last_seen=_dt(d.get("last_seen")),
            settings=merged,
        )


def user_id(host: str, account_id: str | int) -> str:
    """The stable key for an account. The numeric id, never the login: GitHub
    logins are renameable and, once released, re-registrable by a stranger."""
    return f"{(host or 'github.com').strip().lower()}:{account_id}"
