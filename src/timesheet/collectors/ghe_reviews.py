"""GitHub Enterprise PR reviews -> raw review events for normalize.py.

Code review is real work that leaves no commit on the default branch, so the
commit-only effort model can't see it (for sargea50, ~49 reviews a week are
invisible). This finds the PRs the user reviewed in the window, then reads each
PR's reviews for the exact `submitted_at`, and emits them as review events
(kind="review") that join the commit timeline in reconstruction.

REST only (needs GHE_TOKEN): GraphQL `contributionsCollection` returns zeroes on
this GHES. It is heavier than a commit search (one call per reviewed PR), so it is
run in the background refresh, never a web request (see pipeline.collect_week).
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request


def _get(url: str, token: str) -> list | dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _reviewed_prs(host: str, user: str, since: str, until: str, token: str,
                  max_pages: int = 5) -> list[dict]:
    """PRs the user reviewed, updated in [since, until]. Each: {repo_path, number, title}."""
    q = urllib.parse.quote(f"is:pr reviewed-by:{user} updated:{since}..{until}")
    prs: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (f"https://{host}/api/v3/search/issues"
               f"?q={q}&per_page=100&page={page}")
        items = _get(url, token).get("items", [])
        for it in items:
            repo_path = (it.get("repository_url", "").split("/repos/", 1)[-1])  # owner/repo
            if repo_path and it.get("number"):
                prs.append({"repo_path": repo_path, "number": it["number"],
                            "title": (it.get("title") or "").strip()})
        if len(items) < 100:
            break
    return prs


def fetch_reviews(since: str, until: str, *, host: str | None = None,
                  user: str | None = None, token: str | None = None) -> list[dict]:
    """since/until: 'YYYY-MM-DD' inclusive. Returns raw review dicts
    (ts_local/repo/message/kind='review'); empty if no token (local dev)."""
    host = host or os.environ.get("GHE_HOST", "pinkroccade.ghe.com")
    user = user or os.environ.get("GHE_USER", "sargea50")
    token = token or os.environ.get("GHE_TOKEN")
    if not token:
        return []
    ulow = user.lower()
    out: list[dict] = []
    for pr in _reviewed_prs(host, user, since, until, token):
        repo_name = pr["repo_path"].split("/")[-1]
        url = (f"https://{host}/api/v3/repos/{pr['repo_path']}"
               f"/pulls/{pr['number']}/reviews?per_page=100")
        try:
            reviews = _get(url, token)
        except Exception:  # noqa: BLE001, S112 — one unreadable PR must not sink the whole pull
            continue
        # One PR can carry dozens of review submissions seconds apart (comment
        # threads, re-approvals). Collapse to the EARLIEST review this user left on
        # the PR in the window — one review event per PR, so bursts don't inflate.
        stamps = [rv.get("submitted_at") for rv in (reviews if isinstance(reviews, list) else [])
                  if (rv.get("user") or {}).get("login", "").lower() == ulow]
        stamps = [s for s in stamps if s and since <= s[:10] <= until]
        if stamps:
            out.append({"ts_local": min(stamps), "repo": repo_name,
                        "message": pr["title"] or "Code review", "kind": "review"})
    return out
