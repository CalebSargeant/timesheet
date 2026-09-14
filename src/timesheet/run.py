"""The scheduled refresh: rebuild each account's week, store it, optionally send it.

    python -m timesheet.run                 # every account, this week
    python -m timesheet.run 2026-07-20      # a specific week (any day in it)
    python -m timesheet.run --send          # also deliver to each manager
    python -m timesheet.run --user <id>     # one account only
    python -m timesheet.run --list          # who is registered here

This is where the heavy signals live: PR reviews (a per-PR fan-out) and the mail
and chat reads. A page view can't afford them, so a week viewed in the browser is
built from calendar and commits and then upgraded here.

One account's failure never stops the rest. A scheduled job that serves twenty
people and dies on the first expired token has effectively taken the service down
for the other nineteen, so every account is wrapped and the exit code reports how
many fell over.

Run it from cron, a Kubernetes CronJob, or by hand.
`BACKFILL_WEEKS` (default 6) recent weeks are reconstructed once if not yet stored.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .llm import make_llm
from .pipeline import Sources, collect_week
from .service import crypto, delivery, users
from .service.email import Mailer
from .service.store import GITHUB, MICROSOFT, make_store

log = logging.getLogger("timesheet.run")


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _sources(store, user: users.User) -> Sources:
    """One account's connected credentials. Mirrors the service's own wiring —
    including persisting the rotated Microsoft refresh token, without which the
    connection dies quietly after this run."""
    from .collectors.github import GitHub
    from .collectors.mcp_client import McpSession

    token = ""
    try:
        token = (store.get_credential(user.id, GITHUB) or {}).get("access_token", "")
    except crypto.CryptoUnavailable:
        log.warning("%s: cannot read the stored GitHub token", user.login)
    login = user.setting("github_user") or user.login
    gh = GitHub(user=login, token=token,
                host=user.setting("github_host") or user.host) if login else None

    session = None
    try:
        tokens = store.get_credential(user.id, MICROSOFT)
    except crypto.CryptoUnavailable:
        tokens = None
    if tokens:
        meta = store.credential_meta(user.id, MICROSOFT) or {}
        session = McpSession(
            tokens=tokens,
            on_rotate=lambda fresh, uid=user.id, acct=meta.get("account", ""):
                store.put_credential(uid, MICROSOFT, fresh, account=acct),
        )
    return Sources(github=gh, m365=session)


def refresh_user(store, user: users.User, week: date, *, env_cfg: Config,
                 send: bool, backfill: int, mailer: Mailer | None = None,
                 public_url: str = "") -> dict:
    """Rebuild one account's week (plus any missing recent weeks) and report."""
    cfg = env_cfg.with_settings(users.for_config(user.settings))
    sources = _sources(store, user)
    llm = make_llm(cfg)

    days = collect_week(week, cfg, sources, llm=llm, full=True)
    monday = days[0].date.date() if days else _monday(week)
    meta = store.save(user.id, monday, {}, days, full=True)
    log.info("%s: week %s — %s across %s days",
             user.login, meta["week_start"], meta["total_hm"], meta["days"])

    for i in range(1, backfill + 1):
        m = _monday(week) - timedelta(days=7 * i)
        if store.is_full(user.id, m):
            continue
        try:
            past = collect_week(m, cfg, sources, llm=llm, full=True)
        except Exception:  # noqa: BLE001 — a backfill is a nicety, not the job
            log.warning("%s: backfill of %s failed", user.login, m, exc_info=True)
            continue
        store.save(user.id, m, {}, past, full=True)
        log.info("%s: backfilled %s (%d days)", user.login, m.isoformat(), len(past))

    result = {"user": user.login, "week": meta["week_start"], "total": meta["total_hm"],
              "delivery": "not requested"}
    if send:
        out = delivery.send(user, days, meta, session=sources.m365, mailer=mailer,
                            public_url=public_url)
        result["delivery"] = out.describe()
        log.info("%s: %s", user.login, out.describe())
    return result


def main(argv: list[str]) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s")
    env_cfg = Config.from_env()
    store = make_store(tz=env_cfg.tz)

    flags = {a for a in argv[1:] if a.startswith("--")}
    positional = [a for a in argv[1:] if not a.startswith("--")]
    only = None
    if "--user" in argv:
        idx = argv.index("--user")
        only = argv[idx + 1] if idx + 1 < len(argv) else None
        positional = [a for a in positional if a != only]

    accounts = store.list_users()
    if "--list" in flags:
        for u in accounts:
            channel, target = delivery.target_for(u)
            print(f"{u.id}\t{u.login}\t{channel}:{target or '-'}")
        return 0
    if only:
        accounts = [u for u in accounts if u.id == only or u.login == only]
        if not accounts:
            print(f"no account matching {only!r}", file=sys.stderr)
            return 2
    if not accounts:
        log.warning("no accounts registered yet — nobody has signed in")
        return 0

    week = date.fromisoformat(positional[0]) if positional \
        else datetime.now(ZoneInfo(env_cfg.tz)).date()
    send = "--send" in flags or "--email" in flags      # --email kept for old cron entries
    backfill = int(os.environ.get("BACKFILL_WEEKS", "6"))
    mailer = Mailer.from_env()
    public_url = os.environ.get("PUBLIC_URL", "")

    failures = 0
    for user in accounts:
        try:
            refresh_user(store, user, week, env_cfg=env_cfg, send=send, backfill=backfill,
                         mailer=mailer, public_url=public_url)
        except Exception:  # noqa: BLE001 — one account must not take down the run
            failures += 1
            log.error("%s: refresh failed", user.login, exc_info=True)

    if failures:
        log.error("%d of %d account(s) failed", failures, len(accounts))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
