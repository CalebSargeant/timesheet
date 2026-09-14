"""GitHub activity -> raw activity dicts for `normalize.py`.

Covers github.com and GitHub Enterprise Server behind one client, because the
only differences are the API base path and which token opens it. Every call is
scoped to one person's credentials: the service builds a client per signed-in
user, so no account can read another's activity through this module.

Three kinds of evidence, deliberately weighted differently downstream:

  * **commits** — the backbone. Timestamps cluster into coding sessions.
  * **PRs opened / issues filed** — one search each, cheap enough for a web
    request, credited per item.
  * **reviews** — real senior work that leaves no commit on the default branch.
    Needs a per-PR fan-out, so it belongs in the background refresh only.

    python -m timesheet.collectors.github 2026-07-20 2026-07-26
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..net import open_url

log = logging.getLogger(__name__)

CLOAK = "application/vnd.github.cloak-preview+json"      # commit search by author-date
JSON_ACCEPT = "application/vnd.github+json"
DOTCOM = "github.com"

# GitHub Enterprise Cloud with data residency. These are GitHub-operated, not
# self-hosted, and they do NOT serve /api/v3 — see `api_base`.
RESIDENCY_SUFFIX = ".ghe.com"


def normalize_host(host: str) -> str:
    """A bare hostname: no scheme, no trailing slash, lower-cased."""
    return (host or "").strip().lower().removeprefix("https://").removeprefix(
        "http://").rstrip("/")


def web_base(host: str) -> str:
    """Where a human signs in. Also the OAuth authorize/token host.

    An `api.` prefix is stripped: somebody who set the host to their API endpoint
    would otherwise be redirected to sign in at a hostname that serves no sign-in
    page, and the failure reads as a broken OAuth app rather than a typo.
    """
    h = normalize_host(host) or DOTCOM
    if h == "api.github.com":
        return f"https://{DOTCOM}"
    if h.endswith(RESIDENCY_SUFFIX):
        h = h.removeprefix("api.")
    return f"https://{h}"


def api_base(host: str) -> str:
    """The REST base for a host. Three shapes, and conflating them 404s every call:

      github.com                -> https://api.github.com
      <tenant>.ghe.com          -> https://api.<tenant>.ghe.com   (Enterprise Cloud
                                   with data residency: GitHub-operated, and there
                                   is no /api/v3 on it)
      anything else             -> https://<host>/api/v3          (Enterprise Server)
    """
    h = normalize_host(host)
    if h in (DOTCOM, "api.github.com", ""):
        return "https://api.github.com"
    if h.endswith(RESIDENCY_SUFFIX):
        return f"https://{h}" if h.startswith("api.") else f"https://api.{h}"
    return f"https://{h}/api/v3"


class GitHubError(RuntimeError):
    """GitHub could not be reached, or refused the request."""


@dataclass(frozen=True)
class GitHub:
    """One person's view of one GitHub host."""
    user: str
    token: str = ""
    host: str = DOTCOM
    timeout: int = 30

    @property
    def api(self) -> str:
        return api_base(self.host)

    @property
    def web(self) -> str:
        return web_base(self.host)

    def get(self, path: str, *, accept: str = JSON_ACCEPT) -> dict | list:
        """One authenticated GET. Falls back to an already-signed-in `gh` CLI when
        no token was supplied, which is what makes local development work without
        minting a PAT."""
        if not self.token:
            return self._via_gh(path, accept)
        req = urllib.request.Request(f"{self.api}{path}", headers={  # noqa: S310 — opened through net.open_url, which enforces https
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with open_url(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise GitHubError(f"GitHub {e.code} on {path.split('?')[0]}") from e
        except urllib.error.URLError as e:
            raise GitHubError(f"cannot reach {self.api}: {e.reason}") from e

    def _via_gh(self, path: str, accept: str) -> dict | list:
        gh = os.environ.get("GH_BIN", "gh")
        host = self.host or DOTCOM
        # No shell, and a fixed argv whose only variable parts are a hostname and
        # an API path — nothing here is interpretable as a shell metacharacter.
        out = subprocess.run(  # noqa: S603  # nosec B603
            [gh, "api", "--hostname", host, "-H", f"Accept: {accept}", path],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if out.returncode != 0:
            raise GitHubError(f"gh api failed: {out.stderr.strip()[:200]}")
        return json.loads(out.stdout)

    def search(self, kind: str, query: str, *, accept: str = JSON_ACCEPT,
               max_pages: int = 5, extra: str = "") -> list[dict]:
        """Paged search. `kind` is 'commits' or 'issues'.

        Stops at `max_pages` rather than following to the end: GitHub caps search
        at 1000 results anyway, and a runaway query must not hold a web request
        open indefinitely.
        """
        items: list[dict] = []
        q = urllib.parse.quote(query)
        for page in range(1, max_pages + 1):
            path = f"/search/{kind}?q={q}&per_page=100&page={page}{extra}"
            got = self.get(path, accept=accept)
            batch = got.get("items", []) if isinstance(got, dict) else []
            items.extend(batch)
            if len(batch) < 100:
                break
        return items


def _from_env(env: dict | None = None) -> GitHub:
    e = env or os.environ
    return GitHub(
        user=e.get("GITHUB_USER", ""),
        token=e.get("GITHUB_TOKEN", ""),
        host=e.get("GITHUB_HOST", DOTCOM),
    )


def fetch_commits(since: str, until: str, *, client: GitHub | None = None) -> list[dict]:
    """since/until: 'YYYY-MM-DD' inclusive. Raw commit dicts (ts_local/repo/message)."""
    gh = client or _from_env()
    if not gh.user:
        return []
    items = gh.search("commits", f"author:{gh.user} author-date:{since}..{until}",
                      accept=CLOAK, extra="&sort=author-date&order=asc")
    out = []
    for it in items:
        commit = it.get("commit", {})
        stamp = (commit.get("author") or {}).get("date")
        if not stamp:
            continue
        out.append({
            "ts_local": stamp,                                   # ISO8601 with offset
            "repo": (it.get("repository") or {}).get("name", "?"),
            "message": (commit.get("message") or "").split("\n")[0],
        })
    return out


def fetch_authored(since: str, until: str, *, client: GitHub | None = None) -> list[dict]:
    """PRs opened + issues filed, as raw activity dicts (kind 'pr' / 'issue').

    Opening a PR and filing an issue are timestamped work a commit search misses
    entirely — a busy review-and-triage day can carry dozens of each and not one
    commit. Unlike reviews this needs no per-item fan-out: `created_at` comes
    back with the search result, so it is one call per kind.
    """
    gh = client or _from_env()
    if not gh.user:
        return []
    out: list[dict] = []
    for kind, filt in (("pr", "is:pr"), ("issue", "is:issue")):
        items = gh.search("issues", f"{filt} author:{gh.user} created:{since}..{until}")
        for it in items:
            created = it.get("created_at")
            if created and since <= created[:10] <= until:
                out.append({
                    "ts_local": created,
                    "repo": it.get("repository_url", "").rsplit("/", 1)[-1],
                    "message": (it.get("title") or "").strip() or kind,
                    "kind": kind,
                })
    return out


def fetch_reviews(since: str, until: str, *, client: GitHub | None = None) -> list[dict]:
    """PR reviews submitted in the window, as raw dicts (kind 'review').

    One review event per PR, not per submission: a single pull request can carry
    dozens of review submissions seconds apart (comment threads, re-approvals),
    and counting each would turn one careful read into half a day.

    Heavier than a commit search — one call per reviewed PR — so this belongs in
    the background refresh, never a web request.
    """
    gh = client or _from_env()
    if not gh.user:
        return []
    low = gh.user.lower()
    out: list[dict] = []
    found = gh.search("issues", f"is:pr reviewed-by:{gh.user} updated:{since}..{until}")
    for it in found:
        repo_path = it.get("repository_url", "").split("/repos/", 1)[-1]   # owner/repo
        number = it.get("number")
        if not repo_path or not number:
            continue
        try:
            reviews = gh.get(f"/repos/{repo_path}/pulls/{number}/reviews?per_page=100")
        except GitHubError:     # one unreadable PR must not sink the whole pull
            log.debug("review fetch failed for %s#%s", repo_path, number, exc_info=True)
            continue
        stamps = [rv.get("submitted_at") for rv in (reviews if isinstance(reviews, list) else [])
                  if (rv.get("user") or {}).get("login", "").lower() == low]
        stamps = [s for s in stamps if s and since <= s[:10] <= until]
        if stamps:
            out.append({"ts_local": min(stamps), "repo": repo_path.split("/")[-1],
                        "message": (it.get("title") or "").strip() or "Code review",
                        "kind": "review"})
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    a, b = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]
    gh = _from_env()
    print(json.dumps({
        "commits": fetch_commits(a, b, client=gh),
        "authored": fetch_authored(a, b, client=gh),
        "reviews": fetch_reviews(a, b, client=gh),
    }, indent=2))
