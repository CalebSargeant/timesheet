# HTTP API

Every route that returns data resolves the signed-in account first and reads only
that account's rows. There is no route here capable of returning one person's week
to another.

Sessions are a signed `ts_session` cookie (`HttpOnly`, `SameSite=Lax`, `Secure`
when `PUBLIC_URL` is https). State-changing `POST`s additionally require a `csrf`
form field, issued with the page that carries the form.

## Signing in

### `GET /auth/login`
Redirects to GitHub's authorisation page.

| Query | |
| --- | --- |
| `next` | Where to land afterwards. A **local path only** — an absolute URL is discarded, because signing one would turn the callback into an open redirect. |

### `GET /auth/callback`
Finishes the exchange, creates or refreshes the account, and sets the session cookie.

| Status | |
| --- | --- |
| `303` | Signed in; redirected to `next` |
| `400` | The `state` expired or was not issued here |
| `403` | The account is real but `ACCESS_POLICY` does not admit it |
| `502` | GitHub refused the code, or could not be reached |

### `POST /auth/logout`
Clears the cookie. Form field: `csrf`.

## The timesheet

### `GET /`
The live page. Signed out, this is the landing page instead — never a timesheet.

| Query | |
| --- | --- |
| `period` | `this-week` (default), `last-week`, `this-month`, `last-month` |
| `from`, `to` | `YYYY-MM-DD`. Both together override `period`. |

A past week is assembled from stored weeks; one that is not cached yet is
reconstructed live and stored.

The **current** week is never frozen. While the scheduled refresh's copy is
younger than `LIVE_MAX_AGE_SECONDS` (default 900) it is served as is — it carries
the signals a page view cannot afford (mail, chat, reviews). Older than that and
the page rebuilds it live from calendar and commits. If that rebuild reads
nothing, the stored copy is shown with a line saying how old it is: a week that is
an hour behind is a worse answer than a live one and a far better answer than a
blank page.

Anything that could not be read is printed above the table — a thin week says why
it is thin, instead of looking like a week in which nobody worked.

| Query | |
| --- | --- |
| `message`, `error` | What a redirect back from `POST /deliver` has to say. Rendered as a note, never as markup. |

### `GET /timesheet.xlsx`
The selected period as a workbook, in the account's own language.

| Query | Same as `GET /` |
| --- | --- |

| Status | |
| --- | --- |
| `200` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |
| `401` | Not signed in |
| `404` | Nothing recorded for that period |

### `GET /d/{uid}/{token}/timesheet.xlsx`
The link sent to a manager. No session: the signature covers the account id, the
filename **and** the expiry, so a valid link cannot be pointed at a different
account by editing the path.

| Status | |
| --- | --- |
| `200` | The workbook |
| `403` | Expired, or not signed for that account |
| `404` | No such account |

## Account

### `GET /connections`
What this account is linked to, and whether delivery is switched on.

### `POST /connect/microsoft`
Starts a device-code sign-in and renders the code. Form field: `csrf`.

The response shows only `user_code` and `verification_uri`. The `device_code` is
the bearer secret of the pending sign-in and never reaches the page.

### `GET /connect/microsoft/poll`
Has the human finished yet?

```json
{ "done": false }
{ "done": true }
{ "done": false, "error": "sign-in failed: authorization_declined" }
```

`done: false` with no `error` means "still waiting" and nothing else. Anything
genuinely wrong comes back as an `error` and the poller stops — treating a
failure as "keep polling" spins until the tab is closed.

### `POST /connect/microsoft/finish`
The same check, once, for a browser with no JavaScript. Form field: `csrf`.

### `POST /connect/microsoft/disconnect`
Deletes the stored token. Form field: `csrf`.

### `GET` / `POST /settings`
The account's own reconstruction settings. Every field is validated against an
explicit allow-list with a type and a bound; one bad value is reported and the
rest of the form is still saved.

### `POST /deliver`
Sends **the period on screen**, once, over the configured channel.

| Form field | |
| --- | --- |
| `csrf` | Required. |
| `period` | `this-week` (default), `last-week`, `this-month`, `last-month` |
| `from`, `to` | `YYYY-MM-DD`. Both together override `period`. |
| `back` | `/` or `/connections` — where to land afterwards. Anything else is replaced with `/connections`, so this cannot become an open redirect. |

The signed link in the message carries the same period, so the manager opens what
the sender was looking at. Redirects back with `message=` or `error=` describing
what actually happened — it never claims a message went out that didn't.

This route ignores *Send my week automatically*: that switch governs the
scheduled run, and a Send button that silently does nothing is its own kind of
dishonest. A channel and a recipient are still required.

### `POST /deliver/test`
Sends a real message, through the real mail server, to the signed-in account —
never to the manager. Same From, same Reply-To, same attachment as the real
thing, subject prefixed `[test]`.

| Form field | |
| --- | --- |
| `csrf` | Required. |
| `to` | Where to send it. Defaults to the account's own email address. |

### `POST /deliver/check`
Proves the mail path without delivering anything. Form field: `csrf`.

- **SMTP:** connects, negotiates TLS, authenticates, hangs up.
- **Mailgun API:** a send with `o:testmode=yes`. Mailgun validates the key, the
  domain and the region, and never delivers the message. A domain sending key
  may call nothing else (Mailgun limits it to `POST /messages` and
  `/messages.mime`), so any read-only check would report the right key as
  rejected. Billed like any other message.

A `401`, `403` or `404` from Mailgun comes back naming the region as the likely
cause. A domain answers only on its own region's API, so a valid key sent to
the other region fails exactly like a wrong one.

### `POST /account/delete`
Removes the account, its stored weeks and its Microsoft token. Form fields:
`csrf`, and `confirm` set to the literal `DELETE`.

## Pushed ingest

### `POST /api/ingest`
Calendar, mail and chat pushed in by an external automation, for tenants that
offer no other way in. Commits are still pulled server-side.

| Header | |
| --- | --- |
| `X-Account` | The account id, as shown on the Connections page |
| `X-Ingest-Token` | That account's token. Derived from `SECRET_KEY`, so it is per account — one leaked automation cannot write into everybody's timesheet. |

```json
{
  "week_start": "2026-07-20",
  "meetings": [
    {
      "subject": "Daily standup",
      "start_utc": "2026-07-20T06:45:00",
      "end_utc": "2026-07-20T07:30:00",
      "all_day": false,
      "show_as": "busy"
    }
  ],
  "emails":          [{ "ts_utc": "2026-07-24T09:10:00Z", "subject": "Re: rollout" }],
  "emails_received": [{ "ts_utc": "2026-07-24T09:20:00Z" }],
  "chat":            [{ "ts_utc": "2026-07-24T09:40:00Z" }]
}
```

| Field | |
| --- | --- |
| `week_start` | Any date in the week. Optional; defaults to today. |
| `meetings` | `start_utc`/`end_utc` are UTC with no offset. `show_as` is `busy`, `free`, `oof` or `tentative`; an all-day event marked `busy` or `oof` owns that whole day as leave. |
| `emails` | Mail **sent**. `ts_utc` or `sent_utc`; `subject` is optional and labels the block. |
| `emails_received` | Mail received. Worth far less per item and capped hard — it proves a *sender* was at their desk. |
| `chat` | Messages **you** sent. `teams` is accepted as an older spelling. |

Correspondence is clipped against that day's meetings before any of it is
credited, so pushing in a reply you typed during a call adds nothing.

```json
{
  "ok": true,
  "week_start": "2026-07-20",
  "total_hm": "40:00",
  "total_minutes": 2400,
  "days": 5,
  "logic_version": 13,
  "full": false,
  "generated_at": "2026-07-24T18:30:00+02:00"
}
```

| Status | |
| --- | --- |
| `200` | Ingested and reconstructed |
| `400` | Body was not a JSON object |
| `401` | Missing or wrong `X-Account` / `X-Ingest-Token` |
| `404` | No such account |

## Operational

### `GET /status`
The signed-in account's last refresh, and whether the current week is keeping up.
Requires a session. Reads the store only — the question "is the refresh job
running?" must be answerable without doing the refresh job's work in the request.

```json
{
  "ok": true,
  "account": "alice",
  "this_week": {
    "week_start": "2026-09-14",
    "stored_at": "2026-09-16T09:35:02+02:00",
    "age_seconds": 240,
    "full": true,
    "total_hm": "23:12",
    "fresh": true,
    "max_age_seconds": 900
  },
  "week_start": "2026-09-14",
  "total_hm": "23:12",
  "generated_at": "2026-09-16T09:35:02+02:00",
  "logic_version": 13,
  "full": true,
  "days": 3,
  "total_minutes": 1392
}
```

`fresh: false` means a page view rebuilds the week live instead of serving this
copy. That is the intended fallback, not a fault — but if it is false for long, the
refresh CronJob is not running.

### `GET /healthz`
`{"ok": true}` whenever the process is alive, with no database call. A database
call here turns a slow database into a crash-loop, which is precisely when the
pod needs to stay up.

## Errors

Everything else is FastAPI's JSON shape:

```json
{ "detail": "link expired or invalid" }
```
