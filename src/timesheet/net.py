"""The one way this project opens a URL.

`urllib` speaks `file://` and `ftp://` as readily as it speaks https, and almost
every URL here is assembled from configuration — a connector endpoint, a tenant,
a published-calendar link somebody pasted into a settings form, an LLM base URL.
Handed one of those, a plain `urlopen` turns a network fetch into a local file
read whose contents are then posted onward, or sent to a model, or parsed as a
calendar.

So there is exactly one opener, it refuses anything but https, and every
collector goes through it.

Plain http is allowed in exactly one situation: when the host cannot be on the
public internet. A developer's localhost, and a service inside the cluster this
is deployed to — `http://litellm.prod-litellm.svc.cluster.local:4000` is the
motivating case — are not the threat being guarded against, and demanding TLS
there would have people disable the check wholesale instead. Everything that
could route over the internet still has to be https.
"""
from __future__ import annotations

import ipaddress
import urllib.error
import urllib.parse
import urllib.request

ALLOWED = ("https",)
LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")

# Kubernetes service DNS. A name under .svc resolves only inside the cluster.
CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local")


def is_private(host: str) -> bool:
    """Is this host unreachable from the public internet?

    True for loopback, for a Kubernetes service name, and for an address in a
    private or link-local range. Anything that does not parse as an address and
    is not a cluster name is assumed public — the safe direction to be wrong in.
    """
    if not host:
        return False
    name = host.strip().lower().strip("[]")
    if name in LOOPBACK or name == "localhost":
        return True
    if name.endswith(CLUSTER_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


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
    if parsed.scheme == "http" and is_private(parsed.hostname or ""):
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
