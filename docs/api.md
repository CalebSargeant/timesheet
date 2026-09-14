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

A week is assembled from stored weeks; one that is not cached yet is reconstructed
live and stored. The **current** week is always rebuilt and never cached — today
grows through the day, so a frozen snapshot would be wrong.

### `GET /timesheet.xlsx`
The selected period as a workbook, in the account's own language.

| Query | Same as `GET /` |
| --- | --- |

| Status | |
| --- | --- |
| `200` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |
| `401` | Not signed in |
| `404` | Nothing recorded for that period |

### `GET /d/{account}/{token}/timesheet.xlsx`
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
Sends this week now, over the configured channel. Form field: `csrf`. Redirects
back to `/connections` with `message=` or `error=` describing what actually
happened — it never claims a message went out that didn't.

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
  "logic_version": 12,
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
The signed-in account's last refresh. Requires a session.

### `GET /healthz`
`{"ok": true}` whenever the process is alive, with no database call. A database
call here turns a slow database into a crash-loop, which is precisely when the
pod needs to stay up.

## Errors

Everything else is FastAPI's JSON shape:

```json
{ "detail": "link expired or invalid" }
```
