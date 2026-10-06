"""The scheduled job's wiring: which credentials it hands the collectors."""
import time

import pytest

from timesheet import run
from timesheet.service import auth
from timesheet.service.store import GITHUB, FileStore
from timesheet.service.users import User, defaults


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-long-enough-to-be-accepted")


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
