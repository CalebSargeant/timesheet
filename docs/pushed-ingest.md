# Pushing signals in from outside

> **Most people do not need this.** Connect Microsoft from the Connections page
> and the service reads calendar, mail and chat itself, with no flow to build and
> no flow to maintain.

This page is for two cases:

- a tenant where even a delegated device-code sign-in is blocked, and
- a source this project has no collector for at all (another chat tool, another
  calendar, a ticketing system you want to count).

The idea is simple: something you run pushes timestamped signals to
`POST /api/ingest`, the service pulls your commits itself, and it reconstructs the
week from both. **The service never holds your Microsoft credentials in this
setup** — whatever you build does.

## The contract

```http
POST https://<your-deployment>/api/ingest
X-Account: github.com:12345
X-Ingest-Token: <that account's token, from the Connections page>
Content-Type: application/json
```

```json
{
  "week_start": "2026-07-20",
  "meetings": [
    { "subject": "Daily standup", "start_utc": "2026-07-20T06:45:00",
      "end_utc": "2026-07-20T07:30:00", "all_day": false, "show_as": "busy" }
  ],
  "emails":          [{ "ts_utc": "2026-07-24T09:10:00Z", "subject": "Re: rollout" }],
  "emails_received": [{ "ts_utc": "2026-07-24T09:20:00Z" }],
  "chat":            [{ "ts_utc": "2026-07-24T09:40:00Z" }]
}
```

The token is **per account**, derived from the server's `SECRET_KEY`. A single
deployment-wide secret would let any automation holding it write into everybody's
timesheet, so there isn't one.

Two details are worth getting right:

- **`start_utc` / `end_utc` are UTC wall-clock with no offset.** The service
  converts to the account's own time zone. Sending local times silently shifts
  every meeting.
- **Keep `all_day` and `show_as` faithful.** An all-day event marked `busy` or
  `oof` is read as **leave** and owns that whole day, which is what stops a day
  off being reconstructed into eight hours of invented admin. All-day items
  marked `free` (a desk booking) are ignored.

Correspondence is clipped against that day's meetings before it is credited, so
pushing in replies you typed during a call adds nothing. That is deliberate — see
the "What it counts" table in the README.

Everything except `meetings` is optional. A payload with meetings alone still
produces a valid sheet; it is just thinner.

## Example: a Power Automate flow

Power Automate's **Office 365 Outlook** and **Microsoft Teams** connectors are
Microsoft's own first-party apps, so they need only your consent — no app
registration and no admin. That makes it the usual escape hatch for a locked-down
tenant.

1. **Trigger — Recurrence.** Weekly, Mon–Fri, late afternoon, in your own time zone.

2. **Compute the week window.** Three *Compose* actions:
   - `Monday` = `startOfDay(addDays(utcNow(), -1 * (if(equals(dayOfWeek(utcNow()),0),6,sub(dayOfWeek(utcNow()),1)))))`
   - `WeekStart` = `formatDateTime(outputs('Monday'), 'yyyy-MM-dd')`
   - `Sunday` = `addDays(outputs('Monday'), 7)`

3. **Office 365 Outlook — Get calendar view of events (V3).**
   Start `outputs('Monday')`, End `outputs('Sunday')`, and under Advanced set
   **Time Zone: UTC** so the times come back in the shape above.

4. **Select** the calendar output into the meeting shape:

   ```
   From:      body('Get_calendar_view_of_events_(V3)')?['value']
   subject:   item()?['subject']
   start_utc: item()?['start']
   end_utc:   item()?['end']
   all_day:   item()?['isAllDay']
   show_as:   item()?['showAs']
   ```

5. *(optional)* **Get emails (V3)** from `Sent Items` since `outputs('Monday')`,
   then **Select** → `[{ "ts_utc": item()?['DateTimeSent'], "subject": item()?['Subject'] }]`.
   Repeat against `Inbox` for `emails_received` if you want it.

6. *(optional)* **Microsoft Teams — Get messages** for the channels you care
   about, **Filter** to your own sender and this week, then **Select** →
   `[{ "ts_utc": item()?['createdDateTime'] }]`.

7. **HTTP — POST** to `/api/ingest` with the two headers and:

   ```
   {
     "week_start": "@{outputs('WeekStart')}",
     "meetings":   @{body('Select_meetings')},
     "emails":     @{body('Select_emails')},
     "chat":       @{body('Select_chat')}
   }
   ```

Save, run once, and the page updates.

### If the HTTP action is out of reach

`HTTP` is a premium connector. Without it, anything that can make an authenticated
`POST` will do — a scheduled GitHub Actions job, a cron script on a machine you
own, an n8n or Zapier step. The contract is the same; the token is the same.
