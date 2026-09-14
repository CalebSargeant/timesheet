"""The one way this project opens a URL.

`urllib` speaks `file://` and `ftp://` as readily as it speaks https, and almost
every URL here is assembled from configuration — a connector endpoint, a tenant,
a published-calendar link somebody pasted into a settings form, an LLM base URL.
Handed one of those, a plain `urlopen` turns a network fetch into a local file
read whose contents are then posted onward, or sent to a model, or parsed as a
calendar.

So there is exactly one opener, it refuses anything but https, and every
collector goes through it. Loopback over plain http is the single exception,
because a developer running something on localhost is not the threat this
guards against.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

ALLOWED = ("https",)
LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")


class InsecureUrl(ValueError):
    """The URL is not something this project is willing to open."""


def check(url: str) -> str:
    """Return `url` if it may be opened, else raise. Also usable as a validator
    for a setting, so a bad value is refused when it is saved rather than at
    three in the morning when a scheduled run reaches it."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as e:
        raise InsecureUrl(f"not a URL: {url[:60]!r}") from e
    if parsed.scheme in ALLOWED:
        return url
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK:
        return url
    raise InsecureUrl(
        f"refusing to open a non-https URL: {url[:60]!r}")


def open_url(target: str | urllib.request.Request, *, data: bytes | None = None,
             headers: dict | None = None, timeout: int = 30):
    """`urlopen`, restricted to https. Accepts a URL or a prepared Request."""
    if isinstance(target, urllib.request.Request):
        check(target.full_url)
        request = target
    else:
        check(target)
        request = urllib.request.Request(  # noqa: S310 — the scheme is checked above
            target, data=data, headers=headers or {})
    # The scheme is checked immediately above, which is the mitigation every
    # scanner asks for here; the markers keep them from re-reporting it.
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosec B310  # nosemgrep
