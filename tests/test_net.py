"""The one opener.

Almost every URL this project fetches is assembled from configuration — a
connector endpoint, a tenant, a published-calendar link pasted into a settings
form, an LLM base URL. `urllib` speaks `file://` as readily as https, so without
this check a pasted value turns a network fetch into a local file read whose
contents are then parsed as a calendar, or posted onward, or sent to a model.
"""
import urllib.request
from datetime import UTC, datetime

import pytest

from timesheet import net
from timesheet.collectors import m365_ics, mcp_client


@pytest.mark.parametrize("url", [
    "https://example.invalid/x",
    "https://example.invalid:8443/x?y=1",
])
def test_https_is_allowed(url):
    assert net.check(url) == url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "file://C:/Windows/win.ini",
    "ftp://example.invalid/x",
    "gopher://example.invalid/x",
    "http://example.invalid/x",
    "//example.invalid/x",             # scheme-relative: no scheme at all
    "",
])
def test_everything_else_is_refused(url):
    with pytest.raises(net.InsecureUrl):
        net.check(url)


@pytest.mark.parametrize("url", [
    "http://localhost:8000/v1",
    "http://127.0.0.1:11434/v1",
    "http://[::1]:8000/v1",
])
def test_loopback_over_plain_http_is_the_one_exception(url):
    """A developer running a model on localhost is not the threat this guards
    against, and demanding a certificate there would just push people to
    disable the check entirely."""
    assert net.check(url) == url


def test_a_non_loopback_host_over_http_is_still_refused():
    """The exception is the host, not the scheme."""
    with pytest.raises(net.InsecureUrl):
        net.check("http://evil.example/pretending-to-be-local")


def test_open_url_refuses_before_it_opens_anything(monkeypatch):
    """The check must happen first: refusing after the fetch is not a control."""
    opened = []
    monkeypatch.setattr(net.urllib.request, "urlopen",
                        lambda *a, **k: opened.append(a) or None)
    with pytest.raises(net.InsecureUrl):
        net.open_url("file:///etc/passwd")
    assert opened == []


def test_open_url_checks_a_prepared_request_too(monkeypatch):
    """Collectors hand in a Request with headers already set; the scheme inside
    it has to be checked just the same."""
    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: "opened")
    bad = urllib.request.Request("file:///etc/passwd")  # noqa: S310 — never opened
    with pytest.raises(net.InsecureUrl):
        net.open_url(bad)
    assert net.open_url(urllib.request.Request("https://example.invalid")) == "opened"


def test_an_insecure_url_is_a_value_error():
    """So a settings form can catch it with everything else it validates."""
    assert issubclass(net.InsecureUrl, ValueError)


def test_the_ics_collector_refuses_a_file_url():
    """The case this exists for: the ICS link comes from a settings form."""
    with pytest.raises(net.InsecureUrl):
        m365_ics.fetch_events("file:///etc/passwd",
                              datetime(2026, 7, 20, tzinfo=UTC),
                              datetime(2026, 7, 27, tzinfo=UTC))


def test_the_mcp_transport_reports_it_as_its_own_error():
    """Callers there catch McpError; a bare ValueError would escape them."""
    with pytest.raises(mcp_client.McpError, match="non-https"):
        mcp_client._urlopen("file:///etc/passwd", data=b"", timeout=1)


# --- plain http, where the host cannot be on the public internet ------------


@pytest.mark.parametrize("url", [
    # The motivating case: the LLM gateway inside the cluster this deploys to.
    "http://litellm.prod-litellm.svc.cluster.local:4000/v1",
    "http://litellm.prod-litellm.svc:4000/v1",
    "http://10.150.0.101:8200",          # private ranges
    "http://172.16.4.4:8200",
    "http://192.168.1.5:4000",
    "http://169.254.169.254/latest",     # link-local
])
def test_plain_http_is_allowed_where_the_host_cannot_be_public(url):
    """Demanding TLS on a ClusterIP would get the check disabled wholesale, which
    is worse than allowing it for hosts that cannot route off the network."""
    assert net.check(url) == url


@pytest.mark.parametrize("url", [
    "http://api.openai.com/v1",
    "http://evil.example/v1",
    # A PUBLIC domain that merely contains .svc must not slip through: the match
    # is on the end of the name, not anywhere in it.
    "http://litellm.svc.evil.example/v1",
    "http://svc.cluster.local.evil.example/v1",
    "http://8.8.8.8/v1",                 # a public address is still public
])
def test_plain_http_over_anything_routable_is_still_refused(url):
    with pytest.raises(net.InsecureUrl):
        net.check(url)


def test_is_private_does_not_guess():
    """A name that is neither a cluster name nor a parseable address is treated
    as public — the safe direction to be wrong in."""
    assert net.is_private("") is False
    assert net.is_private("not-an-address") is False
    assert net.is_private("[::1]") is True
    assert net.is_private("10.0.0.1") is True


def test_an_in_cluster_gateway_is_accepted_by_the_llm_layer():
    """The whole point of the change: this used to raise InsecureUrl and take the
    scheduled refresh down with it rather than degrade."""
    from timesheet.config import Config
    from timesheet.llm import make_llm

    cfg = Config(llm_base_url="http://litellm.prod-litellm.svc.cluster.local:4000/v1",
                 llm_api_key="k", llm_model="deepseek-chat")
    assert make_llm(cfg) is not None


def test_a_public_http_gateway_is_still_refused_by_the_llm_layer():
    """The API key rides as a bearer token on every call."""
    from timesheet.config import Config
    from timesheet.llm import make_llm

    cfg = Config(llm_base_url="http://gateway.example/v1", llm_api_key="k",
                 llm_model="m")
    with pytest.raises(net.InsecureUrl):
        make_llm(cfg)
