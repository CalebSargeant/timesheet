# timesheet

Fills in your timesheet from what you already did, so you never do it by hand.

It reconstructs a defensible working week out of the signals you generate anyway —
calendar, mail, chat, GitHub — publishes it as a live page, and sends it to your
manager as a `.xlsx` by email or as a message in Teams.

Sign in with GitHub. Connect Microsoft. Say who gets the week. That's the whole setup.

```
Monday 20 July  ·  9:51h
  08:30 08:45  0:15  Development       drop dangling service references
  08:45 09:30  0:45  Internal          Daily standup
  09:30 11:00  1:30  Correspondence    Re: production RBAC rollout
  11:00 12:30  1:30  Security          oauth2-proxy --allowed-group uses the bare group name
  12:45 13:00  0:15  Meeting           Weekly business update
  ...
```

## What it counts

Each signal is weighted differently, because they are not worth the same.

| Signal | Where it comes from | How it becomes time |
| --- | --- | --- |
| **Meetings** | calendar | real start and end — fixed anchors the day is built around |
| **Leave** | an all-day busy calendar event | owns the whole day; nothing is reconstructed around it |
| **Commits** | GitHub / GitHub Enterprise | clustered into coding sessions (the session heuristic) |
| **PRs, issues, reviews** | GitHub | credited per item, capped per day |
| **Mail you sent** | Microsoft 365 | clustered into sittings, clipped against meetings, capped |
| **Mail you received** | Microsoft 365 | the same, worth far less — it proves a *sender* was at their desk |
| **Chat you sent** | Microsoft Teams | clustered into sittings, clipped against meetings, capped |

Three rules keep the numbers honest, and they are the reason this is not just a
pretty way of inventing hours:

- **A burst is not an afternoon.** Forty chat messages in ten minutes are worth
  ten minutes plus a lead-in, not forty messages' worth.
- **Correspondence during a meeting is the meeting.** Every sitting is clipped
  against the day's calendar before a single minute of it is credited.
- **Nothing is credited twice, and each source is capped per day.** A mailbox
  that takes 400 messages says more about a distribution list than about you.

Everything is deterministic. AI is optional and only ever polishes a task label,
with a rule-based fallback, and never runs inside a web request.

## Try it offline first

No account, no network, no credentials — reconstruct a captured week and look at
what comes out:

```bash
pip install -e ".[dev]"
python -m timesheet.demo tests/fixtures/week_2026-07-20.json out/
```

`out/timesheet.html` and `out/timesheet.xlsx` are what your manager would see.
Add `nl` as a third argument for the Dutch rendering.

## Run the service

```bash
pip install -e ".[dev,service]"
pytest -q

export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export PUBLIC_URL=http://localhost:8000
export GITHUB_OAUTH_CLIENT_ID=...        # see below
export GITHUB_OAUTH_CLIENT_SECRET=...
uvicorn timesheet.service.main:app --reload
```

Then open <http://localhost:8000> and sign in.

### The GitHub OAuth app

Register one at **Settings → Developer settings → OAuth Apps** (or your Enterprise
Server's equivalent), with:

- **Homepage URL** — your `PUBLIC_URL`
- **Authorization callback URL** — `<PUBLIC_URL>/auth/callback`

For a GitHub Enterprise Server, register the app on the Enterprise Server itself
and set `GITHUB_HOST` to its hostname. Nothing else changes: the only difference
between github.com and GHES here is which hostnames get called.

### Connecting Microsoft

From **Connections**, press *Connect Microsoft*, and enter the code it shows you.
That is the whole flow — no Azure app registration and no admin consent, because
it signs in against an application pair that is already registered multi-tenant.
Every scope is delegated, so it reaches exactly what you can already open in
Outlook and Teams yourself, read-only.

If your tenant blocks even that, there are two other calendar sources: a published
`.ics` link (`M365_ICS_URL`) and a device-code Microsoft Graph sign-in
(`pip install ".[graph]"`). Both give you meetings only, no mail and no chat.
There is deliberately **no fallback between sources on failure** — silently
swapping would make a broken connector look like a week with no meetings, and
that reconstructs into a plausible, entirely wrong, all-admin sheet.

### Who can sign in

`ACCESS_POLICY` decides, and it defaults to `single` — the first account to sign
in claims the deployment and everyone after is refused. That default is
deliberate: this stores delegated mailbox tokens, and a fresh deployment should
not be an open door.

| `ACCESS_POLICY` | Who gets in |
| --- | --- |
| `single` *(default)* | one account — the first to sign in |
| `allowlist` | the logins in `ALLOWED_LOGINS` |
| `org` | members of the orgs in `ALLOWED_ORGS` |
| `open` | anyone with an account on that GitHub host |

## What is stored, and where

Two things in the database would matter if it leaked, and neither is in the clear:
your Microsoft refresh token and your GitHub token. Both are encrypted with
AES-256-GCM under `SECRET_KEY`. With no key set, the service **refuses** to store
them rather than falling back to plaintext.

Every row is owned by exactly one account and every read takes that owner's id, so
there is no query in this codebase capable of returning one person's week or token
to another.

No message bodies are stored. Mail and chat contribute a timestamp and, where the
connector gives one, a subject line used to label a block.

Delete your account from **Settings** and the settings, the stored weeks and the
Microsoft token go with it.

## Scheduled refresh

The web path builds a week from calendar and commits only, because a page view
cannot afford the rest. Run this from cron or a Kubernetes CronJob to fill in the
heavy signals — reviews, mail, chat — and to deliver:

```bash
python -m timesheet.run --send        # every account
python -m timesheet.run --user alice  # one
python -m timesheet.run --list        # who is registered
```

One account's failure never stops the others.

## Deploy

`charts/timesheet` is a Helm chart: a web Deployment, a scheduled CronJob, and a
PostgreSQL DSN you supply. `docker-bake.hcl` builds a multi-arch image. See
[`.env.example`](.env.example) for every setting, and
[`docs/`](docs/) for the HTTP API and the pushed-ingest path.

## Security

Please report anything sensitive privately through
[GitHub's security advisories](https://github.com/calebsargeant/timesheet/security/advisories/new)
rather than opening an issue.

CodeQL and dependency review run on every pull request. Secret scanning and push
protection are GitHub's own and are enabled on the repository, so a credential is
blocked before it lands rather than found afterwards.

## Licence

MIT. See [LICENSE](LICENSE).
