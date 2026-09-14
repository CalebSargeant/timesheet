"""Persistence: accounts, their linked credentials, and their reconstructed weeks.

Every row is owned by exactly one user id and every read takes that id, so there
is no query in this module capable of returning one person's week or token to
another. That is the whole security model of a multi-user deployment, and it is
enforced here rather than in the route handlers, where one forgotten filter would
be a data breach.

Each Mon-Sun week is stored once per user (keyed by its Monday) with its
reconstructed `days`, so the service can assemble any period from stored weeks and
render on demand. The upstream APIs are only hit when a requested week isn't
cached yet.

Two backends behind one interface:
  • PgStore   — PostgreSQL (production): the refresh job writes, the web reads.
  • FileStore — a directory (local dev / tests).
`make_store()` picks Postgres when DATABASE_URL is set, else files.
"""
from __future__ import annotations

import contextlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import RECONSTRUCT_VERSION
from ..model import Day
from ..timeutil import hm
from . import crypto
from .users import User

# A credential is stored encrypted (see crypto.py). The provider names are fixed
# strings rather than an enum so an older row does not become unreadable when a
# provider is added or renamed.
GITHUB = "github"
MICROSOFT = "microsoft"


def _meta(days: list[Day], week_start: date, generated: datetime, full: bool = False) -> dict:
    total = sum(d.minutes for d in days)
    return {"week_start": week_start.isoformat(), "generated_at": generated.isoformat(),
            "total_minutes": total, "total_hm": hm(total), "days": len(days),
            "logic_version": RECONSTRUCT_VERSION, "full": full}


def _is_current(meta: dict | None) -> bool:
    """A cached week is fresh only if it was built by the current reconstruction
    logic. Anything older (or unstamped) is treated as a miss so a deploy quietly
    recomputes it — no manual cache clearing after a logic change."""
    return bool(meta) and meta.get("logic_version") == RECONSTRUCT_VERSION


def _usable(days: list | None) -> bool:
    """Is this cached week worth serving?

    An EMPTY week is never cached as an answer. Everybody signs in before they
    connect anything, so their first page view reconstructs every recent week
    from no sources at all and gets nothing — and caching that froze their whole
    history blank permanently, because a stored `[]` is not None and the current
    logic version matches. Connecting Microsoft afterwards changed nothing.

    Recomputing a genuinely empty week (a fortnight of leave, or before somebody
    joined) costs one cheap pass and still comes back empty. Serving a wrong
    empty one costs somebody their timesheet.
    """
    return bool(days)


def _safe(user_id: str) -> str:
    """A user id as a single filesystem-safe path segment.

    FileStore turns ids into file names and an id is partly attacker-chosen (a
    GitHub host). Anything outside this set — a slash, a dot-dot — would let one
    account's file path escape into another's, so it is replaced rather than
    trusted.
    """
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in user_id)[:120] or "anon"


class FileStore:
    """A directory per deployment; a file per user and per user-week. Dev only —
    there is no locking, so two writers can lose an update."""

    def __init__(self, data_dir: str, tz: str = "UTC"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(tz)

    # --- users ---

    def _user_path(self, user_id: str) -> Path:
        return self.dir / "users" / f"{_safe(user_id)}.json"

    def save_user(self, user: User) -> User:
        p = self._user_path(user.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(p.read_text()) if p.exists() else {}
        doc = {**existing, **user.to_dict()}
        p.write_text(json.dumps(doc, indent=2))
        return User.from_dict(doc)

    def get_user(self, user_id: str) -> User | None:
        p = self._user_path(user_id)
        if not p.exists():
            return None
        return User.from_dict(json.loads(p.read_text()))

    def list_users(self) -> list[User]:
        root = self.dir / "users"
        if not root.exists():
            return []
        out = []
        for p in sorted(root.glob("*.json")):
            try:
                out.append(User.from_dict(json.loads(p.read_text())))
            except (OSError, ValueError, KeyError):
                continue
        return out

    def count_users(self) -> int:
        return len(self.list_users())

    def delete_user(self, user_id: str) -> None:
        self._user_path(user_id).unlink(missing_ok=True)
        self._cred_path(user_id).unlink(missing_ok=True)
        for p in (self.dir / "weeks" / _safe(user_id)).glob("*.json"):
            p.unlink(missing_ok=True)

    # --- credentials ---

    def _cred_path(self, user_id: str) -> Path:
        return self.dir / "creds" / f"{_safe(user_id)}.json"

    def _creds(self, user_id: str) -> dict:
        p = self._cred_path(user_id)
        return json.loads(p.read_text()) if p.exists() else {}

    def put_credential(self, user_id: str, provider: str, payload: dict,
                       account: str = "") -> None:
        p = self._cred_path(user_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = self._creds(user_id)
        doc[provider] = {
            "account": account,
            "blob": crypto.encrypt(json.dumps(payload)),
            "connected_at": datetime.now(self.tz).isoformat(),
        }
        p.write_text(json.dumps(doc))
        with contextlib.suppress(OSError):   # a mode is a nicety, not the point
            p.chmod(0o600)

    def get_credential(self, user_id: str, provider: str) -> dict | None:
        row = self._creds(user_id).get(provider)
        if not row:
            return None
        return json.loads(crypto.decrypt(row["blob"]))

    def credential_meta(self, user_id: str, provider: str) -> dict | None:
        row = self._creds(user_id).get(provider)
        if not row:
            return None
        return {"account": row.get("account", ""), "connected_at": row.get("connected_at")}

    def drop_credential(self, user_id: str, provider: str) -> None:
        doc = self._creds(user_id)
        if doc.pop(provider, None) is not None:
            self._cred_path(user_id).write_text(json.dumps(doc))

    # --- weeks ---

    def _path(self, user_id: str, monday: date) -> Path:
        return self.dir / "weeks" / _safe(user_id) / f"week-{monday.isoformat()}.json"

    def save(self, user_id: str, week_start: date, payload: dict, days: list[Day],
             full: bool = False) -> dict:
        meta = _meta(days, week_start, datetime.now(self.tz), full=full)
        p = self._path(user_id, week_start)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"meta": meta, "days": [d.to_dict() for d in days], "ingest": (payload or {})}))
        return meta

    def _meta_of(self, user_id: str, monday: date) -> dict | None:
        p = self._path(user_id, monday)
        return json.loads(p.read_text()).get("meta") if p.exists() else None

    def get_days(self, user_id: str, monday: date) -> list[Day] | None:
        p = self._path(user_id, monday)
        if not p.exists():
            return None
        doc = json.loads(p.read_text())
        if not _is_current(doc.get("meta")) or not _usable(doc.get("days")):
            return None
        return [Day.from_dict(x) for x in doc["days"]]

    def is_full(self, user_id: str, monday: date) -> bool:
        m = self._meta_of(user_id, monday)
        return _is_current(m) and bool(m.get("full"))

    def latest_meta(self, user_id: str) -> dict:
        metas = []
        for p in (self.dir / "weeks" / _safe(user_id)).glob("week-*.json"):
            try:
                metas.append(json.loads(p.read_text())["meta"])
            except (OSError, ValueError, KeyError):
                continue
        return max(metas, key=lambda m: m.get("generated_at", ""), default={})


_DDL = """
CREATE TABLE IF NOT EXISTS timesheet_user (
    id          text PRIMARY KEY,
    login       text NOT NULL,
    host        text NOT NULL,
    name        text NOT NULL DEFAULT '',
    email       text NOT NULL DEFAULT '',
    avatar_url  text NOT NULL DEFAULT '',
    is_admin    boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz,
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS timesheet_credential (
    user_id      text NOT NULL REFERENCES timesheet_user(id) ON DELETE CASCADE,
    provider     text NOT NULL,
    account      text NOT NULL DEFAULT '',
    blob         text NOT NULL,
    connected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider)
);

CREATE TABLE IF NOT EXISTS timesheet_week (
    user_id      text NOT NULL REFERENCES timesheet_user(id) ON DELETE CASCADE,
    week_start   date NOT NULL,
    generated_at timestamptz NOT NULL,
    total_min    integer NOT NULL,
    meta         jsonb NOT NULL,
    ingest       jsonb,
    days         jsonb,
    PRIMARY KEY (user_id, week_start)
);
"""


class PgStore:
    def __init__(self, dsn: str, tz: str = "UTC"):
        self.dsn = dsn
        self.tz = ZoneInfo(tz)
        with self._conn() as c:
            c.execute(_DDL)

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True)

    @staticmethod
    def _json(value):
        """psycopg returns jsonb as a dict on some versions and as text on others."""
        if value is None or isinstance(value, dict | list):
            return value
        return json.loads(value)

    # --- users ---

    def save_user(self, user: User) -> User:
        with self._conn() as c:
            c.execute(
                """INSERT INTO timesheet_user
                       (id, login, host, name, email, avatar_url, is_admin, last_seen, settings)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET
                       login=EXCLUDED.login, host=EXCLUDED.host, name=EXCLUDED.name,
                       email=EXCLUDED.email, avatar_url=EXCLUDED.avatar_url,
                       is_admin=EXCLUDED.is_admin, last_seen=EXCLUDED.last_seen,
                       settings=EXCLUDED.settings""",
                (user.id, user.login, user.host, user.name, user.email, user.avatar_url,
                 user.is_admin, user.last_seen or datetime.now(self.tz),
                 json.dumps(user.settings)),
            )
        return self.get_user(user.id) or user

    def _row_to_user(self, row) -> User:
        return User.from_dict({
            "id": row[0], "login": row[1], "host": row[2], "name": row[3], "email": row[4],
            "avatar_url": row[5], "is_admin": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
            "last_seen": row[8].isoformat() if row[8] else None,
            "settings": self._json(row[9]) or {},
        })

    _USER_COLS = ("id, login, host, name, email, avatar_url, is_admin, "
                  "created_at, last_seen, settings")

    def get_user(self, user_id: str) -> User | None:
        with self._conn() as c:
            # _USER_COLS is a class constant, never anything a request supplies.
            row = c.execute(
                f"SELECT {self._USER_COLS} FROM timesheet_user WHERE id=%s",  # noqa: S608
                (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        with self._conn() as c:
            # _USER_COLS is a class constant, never anything a request supplies.
            rows = c.execute(
                f"SELECT {self._USER_COLS} FROM timesheet_user "  # noqa: S608
                "ORDER BY created_at").fetchall()
        return [self._row_to_user(r) for r in rows]

    def count_users(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT count(*) FROM timesheet_user").fetchone()[0]

    def delete_user(self, user_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM timesheet_user WHERE id=%s", (user_id,))

    # --- credentials ---

    def put_credential(self, user_id: str, provider: str, payload: dict,
                       account: str = "") -> None:
        blob = crypto.encrypt(json.dumps(payload))
        with self._conn() as c:
            c.execute(
                """INSERT INTO timesheet_credential (user_id, provider, account, blob,
                                                     connected_at)
                   VALUES (%s,%s,%s,%s,now())
                   ON CONFLICT (user_id, provider) DO UPDATE SET
                       account=EXCLUDED.account, blob=EXCLUDED.blob,
                       connected_at=EXCLUDED.connected_at""",
                (user_id, provider, account, blob),
            )

    def get_credential(self, user_id: str, provider: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT blob FROM timesheet_credential WHERE user_id=%s AND provider=%s",
                (user_id, provider)).fetchone()
        return json.loads(crypto.decrypt(row[0])) if row else None

    def credential_meta(self, user_id: str, provider: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT account, connected_at FROM timesheet_credential "
                "WHERE user_id=%s AND provider=%s", (user_id, provider)).fetchone()
        if not row:
            return None
        return {"account": row[0], "connected_at": row[1].isoformat() if row[1] else None}

    def drop_credential(self, user_id: str, provider: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM timesheet_credential WHERE user_id=%s AND provider=%s",
                      (user_id, provider))

    # --- weeks ---

    def save(self, user_id: str, week_start: date, payload: dict, days: list[Day],
             full: bool = False) -> dict:
        meta = _meta(days, week_start, datetime.now(self.tz), full=full)
        with self._conn() as c:
            c.execute(
                """INSERT INTO timesheet_week
                       (user_id, week_start, generated_at, total_min, meta, ingest, days)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (user_id, week_start) DO UPDATE SET
                       generated_at=EXCLUDED.generated_at, total_min=EXCLUDED.total_min,
                       meta=EXCLUDED.meta, ingest=EXCLUDED.ingest, days=EXCLUDED.days""",
                (user_id, week_start, meta["generated_at"], meta["total_minutes"],
                 json.dumps(meta), json.dumps(payload or {})[:2_000_000],
                 json.dumps([d.to_dict() for d in days])),
            )
        return meta

    def get_days(self, user_id: str, monday: date) -> list[Day] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT days, meta FROM timesheet_week WHERE user_id=%s AND week_start=%s",
                (user_id, monday)).fetchone()
        if not row or row[0] is None:
            return None
        raw = self._json(row[0])
        if not _is_current(self._json(row[1])) or not _usable(raw):
            return None
        return [Day.from_dict(x) for x in raw]

    def is_full(self, user_id: str, monday: date) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT meta FROM timesheet_week WHERE user_id=%s AND week_start=%s",
                (user_id, monday)).fetchone()
        if not row or row[0] is None:
            return False
        meta = self._json(row[0])
        return _is_current(meta) and bool(meta.get("full"))

    def latest_meta(self, user_id: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT meta FROM timesheet_week WHERE user_id=%s "
                "ORDER BY generated_at DESC LIMIT 1", (user_id,)).fetchone()
        return self._json(row[0]) if row else {}


def make_store(tz: str = "UTC"):
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return PgStore(dsn, tz=tz)
    return FileStore(os.environ.get("DATA_DIR", "./data"), tz=tz)
