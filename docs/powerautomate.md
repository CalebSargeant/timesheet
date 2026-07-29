# Power Automate → `uren.calebsargeant.com` ingest

Device-code / app-registration auth is locked down in the LOCGOV tenant
(`AADSTS65002` / `700016` — Microsoft no longer preauthorizes third-party client
IDs for Graph). Power Automate sidesteps this entirely: its **Office 365 Outlook**
and **Microsoft Teams** connectors are Microsoft's own first-party apps, so they
need only *your* consent — no app registration, no admin.

The flow runs in your M365, pulls calendar + sent mail + Teams activity, and
`POST`s them to the service. The service adds your GHE commits (pulled server-side
with a PAT) and reconstructs the sheet. **The service never holds Microsoft creds.**

## Ingest contract

```
POST https://uren.calebsargeant.com/api/ingest
Header: X-Ingest-Token: <the shared secret, also set as INGEST_TOKEN on the service>
Content-Type: application/json

{
  "week_start": "2026-07-20",          // Monday of the week (yyyy-MM-dd)
  "meetings": [                        // REQUIRED — drives the reconstruction
    { "subject": "Daily standup", "start_utc": "2026-07-20T06:45:00",
      "end_utc": "2026-07-20T07:30:00", "all_day": false, "show_as": "busy" }
  ],
  "emails": [ { "sent_utc": "2026-07-24T12:10:00Z" } ],   // optional enrichment
  "teams":  [ { "ts_utc":  "2026-07-24T12:40:00Z" } ]     // optional enrichment
}
```

`start_utc`/`end_utc` must be **UTC** wall-clock (no offset) — the service converts
to Europe/Amsterdam. Emails/Teams only refine the label of admin blocks; leaving
them out still produces a valid sheet.

## Build the flow (10 minutes, once)

1. **Trigger — Recurrence.** Frequency Week, on Mon–Fri, at ~18:30
   `Europe/Amsterdam`. (Or Day / 18:30 and let weekends produce an empty week.)

2. **Compute the week window** — add three *Compose* actions:
   - `Monday`  = `startOfDay(addDays(utcNow(), -1 * (if(equals(dayOfWeek(utcNow()),0),6,sub(dayOfWeek(utcNow()),1)))))`
   - `WeekStart` = `formatDateTime(outputs('Monday'), 'yyyy-MM-dd')`
   - `Sunday`  = `addDays(outputs('Monday'), 7)`

3. **Office 365 Outlook — Get calendar view of events (V3).**
   - Calendar Id: Calendar
   - Start Time: `outputs('Monday')`   End Time: `outputs('Sunday')`
   - Advanced → **Time Zone: UTC** (so Start/End come back as UTC).

4. **Select (Data Operation)** — map calendar output to the meeting shape:
   ```
   From:      body('Get_calendar_view_of_events_(V3)')?['value']
   subject:   item()?['subject']
   start_utc: item()?['start']
   end_utc:   item()?['end']
   all_day:   item()?['isAllDay']
   show_as:   item()?['showAs']
   ```

5. *(optional)* **Office 365 Outlook — Get emails (V3)**, Folder `Sent Items`,
   received after `outputs('Monday')`. Then a **Select** →
   `[{ "sent_utc": item()?['DateTimeSent'] }]`.

6. *(optional, Teams)* **Microsoft Teams — Get messages** for the Team Cloud
   channel(s) you care about; **Filter** to your own sender and this week; **Select**
   → `[{ "ts_utc": item()?['createdDateTime'] }]`. Teams is enrichment only; skip
   for v1 if it's fiddly.

7. **HTTP — POST** *(see the licence note below)*
   - URI: `https://uren.calebsargeant.com/api/ingest`
   - Headers: `X-Ingest-Token: <secret>`, `Content-Type: application/json`
   - Body:
     ```
     {
       "week_start": "@{outputs('WeekStart')}",
       "meetings":   @{body('Select_meetings')},
       "emails":     @{body('Select_emails')},
       "teams":      @{body('Select_teams')}
     }
     ```

That's it — save, Run once, and the live page updates.

## The one caveat: the HTTP action is a *premium* connector

If your plan doesn't include premium connectors, two no-premium alternatives — the
service supports both:

- **OneDrive file (standard connector).** Replace step 7 with *OneDrive for
  Business → Create file* writing the JSON to a fixed path, then create a
  **sharing link once** and give it to the service as `INGEST_PULL_URL`; a small
  server-side poller reads that link instead. (No secret in the flow.)
- **Scheduled email (standard connector).** The flow emails the JSON to a
  Cloudflare Email Routing address that forwards to `/api/ingest` via a Worker.

Tell me which of the three you can use and I'll finish that leg. HTTP is simplest
if you have it.
```
