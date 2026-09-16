"""The store: per-user isolation, encrypted credentials, and the version-stamped
cache that makes a logic change invalidate old rows automatically, so a deploy
never serves stale numbers and needs no manual clear."""
from datetime import UTC, date, datetime

import pytest

import timesheet.service.store as store_mod
from timesheet.model import Block, Day
from timesheet.service.store import GITHUB, MICROSOFT, FileStore, make_store
from timesheet.service.users import User, defaults

KEY = "a-test-secret-key-long-enough-to-be-accepted"
ALICE = "github.com:1"
BOB = "github.com:2"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", KEY)


def _day(monday: date) -> Day:
    s = datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=UTC)
    return Day(date=s, blocks=[Block(s, s.replace(hour=17), "Development", "a thing", "focus")])


def _user(uid: str, login: str) -> User:
    return User(id=uid, login=login, settings=defaults())


def test_get_days_roundtrips_current_version(tmp_path):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(ALICE, monday, {}, [_day(monday)])
    got = s.get_days(ALICE, monday)
    assert got is not None and len(got) == 1


def test_stale_version_is_a_miss(tmp_path, monkeypatch):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(ALICE, monday, {}, [_day(monday)])   # stamped with the current version
    # a later deploy bumps the reconstruction logic:
    monkeypatch.setattr(store_mod, "RECONSTRUCT_VERSION", store_mod.RECONSTRUCT_VERSION + 1)
    assert s.get_days(ALICE, monday) is None    # the old row now counts as a cache miss


def test_is_full_reflects_saved_flag(tmp_path):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(ALICE, monday, {}, [_day(monday)], full=False)   # a fast web-warmed week
    assert s.is_full(ALICE, monday) is False                # so the refresh will upgrade it
    s.save(ALICE, monday, {}, [_day(monday)], full=True)    # the scheduled refresh ran
    assert s.is_full(ALICE, monday) is True


def test_one_users_week_is_not_visible_to_another(tmp_path):
    """The whole security model of a multi-user deployment. Every read takes an
    owner and there is no query here capable of crossing accounts."""
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(ALICE, monday, {}, [_day(monday)])
    assert s.get_days(BOB, monday) is None
    assert s.latest_meta(BOB) == {}


def test_a_user_id_cannot_escape_its_own_directory(tmp_path):
    """A user id carries an attacker-choosable GitHub host. Left untouched it is
    a path segment, and '../' in one would read another account's files."""
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save("../../etc:1", monday, {}, [_day(monday)])
    assert not (tmp_path.parent.parent / "etc:1").exists()
    assert list((tmp_path / "weeks").iterdir())


@pytest.mark.parametrize("uid", ["..", ".", "..."])
def test_an_id_made_of_dots_is_not_a_directory(tmp_path, uid):
    """The character filter keeps dots, because hosts have them — so `..` used to
    come through intact, and since each account's weeks live in a directory of
    their own, `weeks/../week-*.json` landed in the data root instead."""
    s = FileStore(str(tmp_path / "data"))
    monday = date(2026, 7, 20)
    s.save(uid, monday, {}, [_day(monday)])
    weeks = tmp_path / "data" / "weeks"
    assert not list((tmp_path / "data").glob("week-*.json"))
    assert [p.name for p in weeks.iterdir()] == ["_" * len(uid)]
    s.delete_user(uid)                     # and deleting it stays inside too
    assert (tmp_path / "data" / "weeks").exists()


def test_credentials_are_encrypted_at_rest(tmp_path):
    s = FileStore(str(tmp_path))
    s.put_credential(ALICE, MICROSOFT, {"refresh_token": "super-secret"}, account="a@x.invalid")
    on_disk = (tmp_path / "creds").glob("*.json")
    assert "super-secret" not in "".join(p.read_text() for p in on_disk)
    assert s.get_credential(ALICE, MICROSOFT) == {"refresh_token": "super-secret"}
    assert s.credential_meta(ALICE, MICROSOFT)["account"] == "a@x.invalid"


def test_a_credential_is_unreadable_with_a_different_key(tmp_path, monkeypatch):
    """AES-GCM authenticates as well as encrypts, so a wrong key is a failed tag
    check, not a plausible-looking wrong answer."""
    from cryptography.exceptions import InvalidTag

    s = FileStore(str(tmp_path))
    s.put_credential(ALICE, GITHUB, {"access_token": "gho_x"})
    monkeypatch.setenv("SECRET_KEY", "a-completely-different-secret-key-here")
    with pytest.raises(InvalidTag):
        s.get_credential(ALICE, GITHUB)


def test_dropping_a_credential_leaves_the_others(tmp_path):
    s = FileStore(str(tmp_path))
    s.put_credential(ALICE, GITHUB, {"access_token": "g"})
    s.put_credential(ALICE, MICROSOFT, {"refresh_token": "m"})
    s.drop_credential(ALICE, MICROSOFT)
    assert s.get_credential(ALICE, MICROSOFT) is None
    assert s.get_credential(ALICE, GITHUB) == {"access_token": "g"}


def test_users_round_trip_with_their_settings(tmp_path):
    s = FileStore(str(tmp_path))
    user = _user(ALICE, "alice")
    user.settings["locale"] = "nl"
    user.settings["manager_email"] = "boss@example.invalid"
    s.save_user(user)
    got = s.get_user(ALICE)
    assert got.login == "alice"
    assert got.settings["locale"] == "nl"
    assert got.settings["manager_email"] == "boss@example.invalid"
    assert s.count_users() == 1


def test_deleting_an_account_takes_its_weeks_and_tokens_with_it(tmp_path):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save_user(_user(ALICE, "alice"))
    s.save(ALICE, monday, {}, [_day(monday)])
    s.put_credential(ALICE, MICROSOFT, {"refresh_token": "m"})
    s.delete_user(ALICE)
    assert s.get_user(ALICE) is None
    assert s.get_days(ALICE, monday) is None
    assert s.credential_meta(ALICE, MICROSOFT) is None


def test_make_store_defaults_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert isinstance(make_store(), FileStore)


# --- an empty week is a missing input, not a fact ---------------------------


def test_an_empty_week_is_never_served_from_the_cache(tmp_path):
    """The first-run trap. Everybody signs in before they connect anything, so
    their first page view reconstructs every recent week from no sources and
    gets nothing. Caching that froze their whole history blank permanently: a
    stored [] is not None and the logic version matches, so connecting
    Microsoft afterwards changed nothing."""
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(ALICE, monday, {}, [])            # what a not-yet-connected account yields
    assert s.get_days(ALICE, monday) is None  # a miss, so it gets recomputed


def test_a_week_with_days_is_still_cached(tmp_path):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(ALICE, monday, {}, [_day(monday)])
    assert s.get_days(ALICE, monday) is not None


def test_an_empty_week_does_not_mask_a_later_real_one(tmp_path):
    """The sequence that actually happens: sign in, get nothing, connect
    Microsoft, come back."""
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(ALICE, monday, {}, [])
    assert s.get_days(ALICE, monday) is None
    s.save(ALICE, monday, {}, [_day(monday)])
    got = s.get_days(ALICE, monday)
    assert got is not None and len(got) == 1
