# github-timesheet

Fills in the daily PinkRoccade **"Uren"** timesheet for me, so I never do it by hand.
It reconstructs a believable working week from signals I already generate and
publishes it live for my manager — plus a downloadable `.xlsx` in his exact template.

## What it does

For each workday it lays out a chronological timeline of blocks
(`Datum · Van · Tot · Duur · Project/klant · Taak`) from:

- **Calendar** (a published Outlook **ICS** link) → meetings become fixed anchors
  (standup, sprint planning, …) at their real local times. An **all-day event marked
  busy** owns its whole day instead: leave (AFAS pushes "Leave / Verlof" into Outlook)
  is reported as one 8h `Verlof` row, not reconstructed into a padded day of admin.
- **A morning on-call rota** → the fixed 07:30 "ochtenddienst / Checks en standby".
- **GitHub-Enterprise commits** (`pinkroccade.ghe.com`) → focus blocks anchored to
  when the work actually happened, labelled from the commit content
  (Security / Monitoring / CI-CD / …).
- Remaining time → `Administratie` blocks, optionally relabelled from **email/Teams**
  activity pushed in later (Power Automate / n8n → `/api/ingest`).

Everything is **deterministic**; AI (DeepSeek via a LiteLLM proxy) is optional and
only polishes task labels, with a rule-based fallback.

## Where the calendar comes from

`M365_SOURCE` picks the collector: `mcp`, `ics` or `graph`. Unset keeps the old
behaviour (`ics` if `M365_ICS_URL` is set, else `graph`). It never falls back on
failure — a silent swap would make a broken source look like a week with no
meetings, which reconstructs into a plausible, wrong, all-admin sheet.

The tenant blocks the device-code Graph path (`AADSTS65002` / `700016`) and I
can't register an Azure AD app, which is what pushed this onto a published ICS
link in the first place.

**`mcp` is the one to use now.** Claude's Microsoft 365 connector is a plain MCP
server that validates an Entra token for an app pair Anthropic registered
multi-tenant and the tenant has *already* consented to — so a device-code
sign-in against that pair is not blocked and needs no admin. Every scope is
delegated, so it reaches exactly what I can already open in Outlook and Teams.
Over the ICS link it adds:

- **Real subjects.** A published calendar set to "availability only" hides every
  subject behind a bare `Busy`, which is why `leave_blank_subjects` exists.
- **No rolling three-month window**, so backfilled months are not silently empty.
- **No world-readable secret URL.**
- **Email and Teams activity**, which ICS cannot give at all — this is what
  `/api/ingest` and the Power Automate flow were built to push in, and the
  nightly refresh now reads it directly. See
  [docs/powerautomate.md](docs/powerautomate.md), still supported, no longer
  needed.

```bash
python -m timesheet.collectors.mcp_client login    # once, interactive
export M365_SOURCE=mcp
python -m timesheet.collectors.m365_mcp 2026-09-01 2026-09-06   # see the raw pull
```

The cache it writes holds a refresh token: standing read access to the mailbox
that never re-prompts for MFA. Treat it like a password, keep it in the vault as
`M365_MCP_TOKEN_JSON`, and use it at least once every 90 days or it ages out.

## Run it locally

```bash
pip install -e ".[dev,service]"
pytest -q

# reconstruct a week from a captured fixture (offline, no network)
python -m timesheet.demo tests/fixtures/week_2026-07-20.json out/

# reconstruct live (calendar + mail + Teams from the MCP connector,
# commits from GHE via gh)
python -m timesheet.collectors.mcp_client login    # once
export M365_SOURCE=mcp
python -m timesheet.run 2026-07-20      # add --email to send it

# or the ICS route
export M365_SOURCE=ics
export M365_ICS_URL='https://outlook.office365.com/owa/calendar/.../calendar.ics'
```

The web service (`uvicorn timesheet.service.main:app`) serves the live page at `/`,
the sheet at `/uren.xlsx`, and accepts pushed enrichment at `POST /api/ingest`.

## Deploy

`charts/github-timesheet` — a web Deployment (reads) + a nightly CronJob (writes),
sharing the firefly **CNPG**; secrets from **OCI Vault** via external-secrets;
`uren.calebsargeant.com` fronted by the Cloudflare tunnel + Access (email-OTP for me
and my manager). See `.env.example` for every knob.

Private internal tool. Not affiliated with or endorsed by PinkRoccade.
