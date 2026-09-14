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
