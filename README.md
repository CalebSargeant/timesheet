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

## Why ICS and not Graph

The LOCGOV tenant blocks the device-code / first-party-client Graph path
(`AADSTS65002` / `700016`) and I can't register an Azure AD app. A published
calendar ICS needs none of that. Email/Teams enrichment (which ICS can't give) can
be pushed in via Power Automate or n8n — see [docs/powerautomate.md](docs/powerautomate.md).

## Run it locally

```bash
pip install -e ".[dev,service]"
pytest -q

# reconstruct a week from a captured fixture (offline, no network)
python -m timesheet.demo tests/fixtures/week_2026-07-20.json out/

# reconstruct live (calendar from your ICS, commits from GHE via gh)
export M365_ICS_URL='https://outlook.office365.com/owa/calendar/.../calendar.ics'
python -m timesheet.run 2026-07-20      # add --email to send it
```

The web service (`uvicorn timesheet.service.main:app`) serves the live page at `/`,
the sheet at `/uren.xlsx`, and accepts pushed enrichment at `POST /api/ingest`.

## Deploy

`charts/github-timesheet` — a web Deployment (reads) + a nightly CronJob (writes),
sharing the firefly **CNPG**; secrets from **OCI Vault** via external-secrets;
`uren.calebsargeant.com` fronted by the Cloudflare tunnel + Access (email-OTP for me
and my manager). See `.env.example` for every knob.

Private internal tool. Not affiliated with or endorsed by PinkRoccade.
