"""GitHub Enterprise commits -> raw commit dicts for normalize.py.

Self-hosted path: REST search API with a PAT (GHE_TOKEN). Local-dev path: shell
out to an already-authenticated `gh` CLI. The search API needs the cloak-preview
media type to filter by author-date."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request

CLOAK = "application/vnd.github.cloak-preview+json"


def _query(user: str, since: str, until: str) -> str:
    # since/until are YYYY-MM-DD (inclusive range on author-date)
    return f"author:{user} author-date:{since}..{until}"


def _rows_from_items(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        commit = it.get("commit", {})
        repo = (it.get("repository") or {}).get("name", "?")
        out.append({
            "ts_local": commit.get("author", {}).get("date"),   # ISO8601 w/ offset
            "repo": repo,
            "message": (commit.get("message") or "").split("\n")[0],
        })
    return [r for r in out if r["ts_local"]]


def _via_rest(host: str, user: str, since: str, until: str, token: str) -> list[dict]:
    q = urllib.parse.quote(_query(user, since, until))
    url = (f"https://{host}/api/v3/search/commits"
           f"?q={q}&per_page=100&sort=author-date&order=asc")
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": CLOAK,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return _rows_from_items(data.get("items", []))


def _via_gh(host: str, user: str, since: str, until: str) -> list[dict]:
    q = _query(user, since, until)
    path = (f"/search/commits?q={urllib.parse.quote(q)}"
            "&per_page=100&sort=author-date&order=asc")
    gh = os.environ.get("GH_BIN", "gh")
    out = subprocess.run(
        [gh, "api", "--hostname", host, "-H", f"Accept: {CLOAK}", path],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api failed: {out.stderr.strip()[:200]}")
    return _rows_from_items(json.loads(out.stdout).get("items", []))


def fetch_commits(since: str, until: str, *, host: str | None = None,
                  user: str | None = None, token: str | None = None) -> list[dict]:
    """since/until: 'YYYY-MM-DD' inclusive. Returns raw commit dicts (ts_local/repo/message)."""
    host = host or os.environ.get("GHE_HOST", "pinkroccade.ghe.com")
    user = user or os.environ.get("GHE_USER", "sargea50")
    token = token or os.environ.get("GHE_TOKEN")
    if token:
        return _via_rest(host, user, since, until, token)
    return _via_gh(host, user, since, until)
