"""GitHub Enterprise PRs opened + issues authored -> raw activity events.

Opening a PR and filing an issue are timestamped work that a commit search misses
(a day can carry 18 PRs / 20 issues for sargea50 with only a handful of commits).
Unlike reviews this needs no per-item fan-out: the search results carry `created_at`
directly, so it is one call per kind and cheap enough for the web path.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request


def _search(host: str, q: str, token: str, max_pages: int = 5) -> list[dict]:
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (f"https://{host}/api/v3/search/issues"
               f"?q={urllib.parse.quote(q)}&per_page=100&page={page}")
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {token}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            page_items = json.loads(r.read()).get("items", [])
        items.extend(page_items)
        if len(page_items) < 100:
            break
    return items


def _events(items: list[dict], since: str, until: str, kind: str) -> list[dict]:
    out = []
    for it in items:
        created = it.get("created_at")
        repo = it.get("repository_url", "").rsplit("/", 1)[-1]
        if created and since <= created[:10] <= until:
            out.append({"ts_local": created, "repo": repo,
                        "message": (it.get("title") or "").strip() or kind, "kind": kind})
    return out


def fetch_authored(since: str, until: str, *, host: str | None = None,
                   user: str | None = None, token: str | None = None) -> list[dict]:
    """since/until: 'YYYY-MM-DD' inclusive. PRs opened + issues authored as raw
    activity dicts (kind 'pr' / 'issue'); empty if no token (local dev)."""
    host = host or os.environ.get("GHE_HOST", "pinkroccade.ghe.com")
    user = user or os.environ.get("GHE_USER", "sargea50")
    token = token or os.environ.get("GHE_TOKEN")
    if not token:
        return []
    prs = _search(host, f"is:pr author:{user} created:{since}..{until}", token)
    issues = _search(host, f"is:issue author:{user} created:{since}..{until}", token)
    return _events(prs, since, until, "pr") + _events(issues, since, until, "issue")
