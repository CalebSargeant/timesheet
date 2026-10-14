"""The scheduled job's wiring: which credentials it hands the collectors, what it
stores, and what it is willing to send."""
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from timesheet import run
from timesheet.config import Config
from timesheet.model import Block, Day
from timesheet.pipeline import WeekBuild
from timesheet.service import auth
from timesheet.service.store import GITHUB, FileStore
from timesheet.service.users import User, defaults

MONDAY = date(2026, 10, 5)
GITHUB_DOWN = ("GitHub commits: GitHub no longer accepts your sign-in (401 on /search/commits)",)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-long-enough-to-be-accepted")


class FakeMailer:
    configured = True
    sender = "timesheet@example.invalid"

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def _days(monday: date = MONDAY) -> list[Day]:
    s = datetime(monday.year, monday.month, monday.day, 9, tzinfo=UTC)
    return [Day(date=s, blocks=[Block(s, s + timedelta(hours=8), "Development", "work", "focus")])]


def _delivering(store) -> User:
    s = defaults()
    s.update(delivery_channel="email", delivery_enabled=True,
             manager_email="boss@example.invalid")
    return store.save_user(User(id="github.com:1", login="alice",
                                email="alice@example.invalid", settings=s))


def _refresh(store, user, monkeypatch, builds=None, *, send=False, backfill=0, mailer=None):
    """One run, with `collect` answering from `builds` ({monday: WeekBuild}) and
    a clean week for any other. Returns the result and the weeks it built."""
    built = []

    def collect(week, cfg, sources, llm=None, full=False):
        built.append(week)
        return (builds or {}).get(week) or WeekBuild(days=_days(week))

    monkeypatch.setattr(run, "collect", collect)
    result = run.refresh_user(store, user, MONDAY, env_cfg=Config(tz="UTC"), send=send,
                              backfill=backfill, mailer=mailer,
                              public_url="https://ts.example.invalid", provider=auth.Provider())
    return result, built


def test_a_week_missing_github_goes_to_its_owner_not_the_manager(tmp_path, monkeypatch):
    """For a month the Friday run mailed the manager weeks whose GitHub reads had
    all failed, padded to forty hours with admin, and nothing on them said so."""
    store = FileStore(str(tmp_path))
    mailer = FakeMailer()
    result, _ = _refresh(store, _delivering(store), monkeypatch,
                         {MONDAY: WeekBuild(days=_days(), problems=GITHUB_DOWN)},
                         send=True, mailer=mailer)
    assert [m["To"] for m in mailer.sent] == ["alice@example.invalid"]
    assert mailer.sent[0]["Subject"].startswith("[not sent]")
    body = mailer.sent[0].get_content()
    assert "boss@example.invalid" in body and "401 on /search/commits" in body
    assert "https://ts.example.invalid/?from=2026-10-05&to=2026-10-11" in body
    assert result["delivery"].startswith("held back")


def test_a_complete_week_still_goes_to_the_manager(tmp_path, monkeypatch):
    store = FileStore(str(tmp_path))
    mailer = FakeMailer()
    _refresh(store, _delivering(store), monkeypatch, send=True, mailer=mailer)
    assert [m["To"] for m in mailer.sent] == ["boss@example.invalid"]


def test_a_short_week_is_rebuilt_once_everything_reads_again(tmp_path, monkeypatch):
    store = FileStore(str(tmp_path))
    user = _delivering(store)
    last = MONDAY - timedelta(days=7)
    store.save(user.id, last, {}, _days(last), full=True, problems=GITHUB_DOWN)
    _, built = _refresh(store, user, monkeypatch, backfill=1)
    assert built == [MONDAY, last]
    assert store.is_full(user.id, last)


def test_no_older_week_is_rebuilt_while_a_source_is_failing(tmp_path, monkeypatch):
    """It would fail the same way, and store another short week."""
    store = FileStore(str(tmp_path))
    user = _delivering(store)
    _, built = _refresh(store, user, monkeypatch,
                        {MONDAY: WeekBuild(days=_days(), problems=GITHUB_DOWN)}, backfill=6)
    assert built == [MONDAY]
    assert store.meta_of(user.id, MONDAY)["problems"] == list(GITHUB_DOWN)


def test_the_job_renews_an_expiring_github_token(tmp_path, monkeypatch):
    """The refresh job is the reader that runs all day, so it is the one that found
    every account's token dead eight hours after that account signed in."""
    store = FileStore(str(tmp_path))
    user = store.save_user(User(id="github.com:1", login="alice", settings=defaults()))
    store.put_credential(user.id, GITHUB, {"access_token": "ghu_old", "refresh_token": "ghr_old",
                                           "expires_at": time.time() - 1}, account="alice")
    monkeypatch.setattr(auth, "_post_json", lambda url, data, timeout=20: {
        "access_token": "ghu_new", "expires_in": 28800, "refresh_token": "ghr_new"})
    provider = auth.Provider(client_id="cid", client_secret="s", host="github.com")

    sources = run._sources(store, user, provider)

    assert sources.github.token == "ghu_new"
    assert store.get_credential(user.id, GITHUB)["refresh_token"] == "ghr_new"
