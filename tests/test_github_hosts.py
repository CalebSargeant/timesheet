"""Which hostname a GitHub call actually goes to.

Three flavours of GitHub, three different API bases, and conflating them 404s
every single call with an error that looks like a permissions problem:

  * **github.com** — API on a separate hostname, `api.github.com`.
  * **`<tenant>.ghe.com`** — Enterprise Cloud with data residency. GitHub-operated,
    API on `api.<tenant>.ghe.com`, and there is no `/api/v3` on it at all.
  * **anything else** — Enterprise Server, self-hosted, API at `/api/v3` on the
    same host.

The middle case is the one that bites: it *looks* like a private enterprise
hostname, so the obvious code treats it as Enterprise Server and quietly builds a
URL that does not exist.
"""
import pytest

from timesheet.collectors.github import GitHub, api_base, normalize_host, web_base
from timesheet.service.auth import Provider


@pytest.mark.parametrize(("host", "expected"), [
    ("github.com", "https://api.github.com"),
    ("api.github.com", "https://api.github.com"),
    ("", "https://api.github.com"),
    # Enterprise Cloud with data residency.
    ("acme.ghe.com", "https://api.acme.ghe.com"),
    ("ACME.GHE.COM", "https://api.acme.ghe.com"),
    ("https://acme.ghe.com/", "https://api.acme.ghe.com"),
    ("api.acme.ghe.com", "https://api.acme.ghe.com"),
    # Enterprise Server.
    ("github.acme.example", "https://github.acme.example/api/v3"),
    ("https://github.acme.example", "https://github.acme.example/api/v3"),
])
def test_api_base(host, expected):
    assert api_base(host) == expected


@pytest.mark.parametrize(("host", "expected"), [
    ("github.com", "https://github.com"),
    ("api.github.com", "https://github.com"),
    ("", "https://github.com"),
    ("acme.ghe.com", "https://acme.ghe.com"),
    # An api. prefix is stripped: that host serves no sign-in page, and the
    # failure would read as a broken OAuth app rather than as a typo.
    ("api.acme.ghe.com", "https://acme.ghe.com"),
    ("github.acme.example", "https://github.acme.example"),
])
def test_web_base(host, expected):
    assert web_base(host) == expected


def test_a_residency_tenant_is_not_treated_as_enterprise_server():
    """The regression this file exists for. `<tenant>.ghe.com/api/v3` does not
    exist, so every commit search against it came back empty."""
    assert api_base("acme.ghe.com") != "https://acme.ghe.com/api/v3"


def test_normalize_strips_scheme_case_and_slash():
    assert normalize_host("  HTTPS://GitHub.Acme.Example/ ") == "github.acme.example"
    assert normalize_host("http://localhost:3000/") == "localhost:3000"


def test_the_client_and_the_oauth_provider_agree():
    """They resolve hosts independently; a divergence means signing in against
    one GitHub and reading activity from another."""
    for host in ("github.com", "acme.ghe.com", "github.acme.example"):
        assert GitHub(user="u", host=host).api == Provider(host=host).api
        assert GitHub(user="u", host=host).web == Provider(host=host).web


def test_the_authorize_url_uses_the_web_host_not_the_api_host():
    p = Provider(client_id="cid", client_secret="s", host="acme.ghe.com",
                 base_url="https://timesheet.example.invalid")
    assert p.authorize_url("STATE").startswith(
        "https://acme.ghe.com/login/oauth/authorize?")


def test_the_callback_is_the_deployments_own_url():
    """Registered verbatim in the OAuth app; a mismatch is a redirect_uri error."""
    p = Provider(base_url="https://timesheet.example.invalid/")
    assert p.redirect_uri == "https://timesheet.example.invalid/auth/callback"


def test_a_rejected_token_says_to_sign_in_again(monkeypatch):
    """This text is shown above the person's own timesheet. A bare status code
    there left three accounts without GitHub for a week and nobody the wiser."""
    import urllib.error

    from timesheet.collectors import github

    def _unauthorised(req, *, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(github, "open_url", _unauthorised)
    with pytest.raises(github.GitHubError, match="sign in again") as caught:
        GitHub(user="alice", token="ghu_dead", host="acme.ghe.com").get("/search/commits?q=x")
    assert "/search/commits" in str(caught.value) and "q=x" not in str(caught.value)
