# API Reference

The service provides a web interface for viewing and downloading timesheet data, plus an endpoint for ingesting enrichment signals.

## Endpoints

### `GET /`
The live timesheet page (HTML).

**Query Parameters:**
- `period`: Time period selector (optional, default: `this-week`)
  - Preset keys: `this-week`, `last-week`, `this-month`, `last-month`, `this-year`, `last-year`
  - Custom range: use `from` and `to` instead
- `from`: Start date in `YYYY-MM-DD` format (for custom period)
- `to`: End date in `YYYY-MM-DD` format (for custom period)

**Response:** HTML page showing the timesheet for the selected period, with navigation and download options.

---

### `GET /uren.xlsx`
Download the timesheet as an Excel file in the PinkRoccade format.

**Query Parameters:** Same as `GET /` (`period`, `from`, `to`)

**Response:** `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` (Excel binary format)

**Filename format:** `Uren-{start_date}_{end_date}.xlsx`

**HTTP Status:**
- `200`: Success
- `404`: No timesheet data for the requested period

---

### `GET /d/{token}/uren.xlsx`
Download the timesheet using a signed (time-limited) URL. Emailed to the manager by the `--email` flag.

**Path Parameters:**
- `token`: Time-limited download token

**Query Parameters:** Same as `GET /` (`period`, `from`, `to`)

**Response:** Excel file (same format as `GET /uren.xlsx`)

**HTTP Status:**
- `200`: Success
- `403`: Link expired or invalid
- `404`: No timesheet data for the requested period

---

### `POST /api/ingest`
Ingest calendar, email, and Teams activity signals from Power Automate (or compatible integration).

**Headers:**
- `X-Ingest-Token`: Shared secret (must match service's `INGEST_TOKEN` environment variable)
- `Content-Type: application/json`

**Request Body:**
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
  "emails": [
    { "sent_utc": "2026-07-24T12:10:00Z" }
  ],
  "teams": [
    { "ts_utc": "2026-07-24T12:40:00Z" }
  ]
}
```

**Fields:**
- `week_start`: Monday of the week (ISO 8601 date: `YYYY-MM-DD`). Required.
- `meetings`: Calendar meetings. Required (can be empty array).
  - `subject`, `start_utc`, `end_utc`: From the calendar (UTC times, no offset).
  - `all_day`: Boolean; true if it's an all-day event.
  - `show_as`: Event availability (`busy`, `free`, `oof`, `tentative`). All-day events marked `busy` or `oof` are treated as full-day leave.
- `emails`: Sent emails (optional). Enrichment only; leaves the day's admin label.
  - `sent_utc`: When the email was sent (UTC timestamp).
- `teams`: Teams messages (optional). Enrichment only.
  - `ts_utc`: When the message was posted (UTC timestamp).

**Response:**
```json
{
  "ok": true,
  "week_start": "2026-07-20",
  "total_hm": "40:00",
  "days": 5,
  "generated_at": "2026-07-24T18:30:00+02:00"
}
```

**HTTP Status:**
- `200`: Success; the week was ingested and reconstructed.
- `400`: Invalid JSON or request body.
- `401`: Missing or invalid `X-Ingest-Token`.

**Details:**
See [Power Automate Integration](powerautomate.md) for instructions on building the Power Automate flow, and the contract examples there.

---

### `GET /status`
Metadata about the last reconstruction: when it ran, how many days, total hours.

**Response:**
```json
{
  "ok": true,
  "week_start": "2026-07-20",
  "total_hm": "40:00",
  "days": 5,
  "generated_at": "2026-07-24T18:30:00+02:00"
}
```

---

### `GET /healthz`
Liveness probe (Kubernetes, uptime monitors).

**Response:**
```json
{ "ok": true }
```

**Details:** Always returns `200` if the process is alive, regardless of database connectivity. This keeps the response fast and prevents false-positive restarts.

---

## Authentication

- **Public page** (`GET /`): No authentication required.
- **Download without link** (`GET /uren.xlsx`): Not protected in this service; fronted by Cloudflare Access in production (email-OTP).
- **Signed download** (`GET /d/{token}/uren.xlsx`): Token-based (emailed links, time-limited).
- **Ingest** (`POST /api/ingest`): Header-based `X-Ingest-Token`.

---

## Errors

All error responses are JSON:

```json
{
  "detail": "Human-readable error message"
}
```

**Common error codes:**
- `400`: Invalid request (bad JSON, wrong format).
- `401`: Missing or invalid authentication token.
- `403`: Permission denied (expired download link).
- `404`: Resource not found (no data for the requested period).
- `500`: Internal server error.
