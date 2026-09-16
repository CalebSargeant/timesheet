# AGENTS.md

**`CLAUDE.md` is canonical.** This file restates the same rules for agents that do not
read it. There is no `@`-import mechanism here, so the substance is repeated rather than
referenced — **edit both files together, or they drift.**

`timesheet` reconstructs a defensible working week from calendar, mail, chat, and GitHub
activity, renders it as a live HTML page, and emails it as a `.xlsx`. Optional AI only
polishes labels and never runs inside a web request.

Before locating unfamiliar code, read `./PROJECT_INDEX.json` first. Agent context files
are in `.claude/`; `COMMON_MISTAKES.md` is always relevant.

## Commands

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
ruff format .

# Offline demo (no network, no account)
python -m timesheet.demo tests/fixtures/week_2026-07-20.json out/

# Service
export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export PUBLIC_URL=http://localhost:8000
export GITHUB_OAUTH_CLIENT_ID=...
export GITHUB_OAUTH_CLIENT_SECRET=...
uvicorn timesheet.service.main:app --reload

# Scheduled refresh / delivery
python -m timesheet.run [--send] [--user alice] [--list]
```

## Architecture

`reconstruct.py` is the pure core (no I/O). Entry points: `pipeline.py:collect` (live
pulls), `pipeline.py:build_week` (pure/testable), `run.py` (CLI),
`service/main.py` (web). `RECONSTRUCT_VERSION` in `config.py` gates the week cache.

Collectors are in `collectors/`; renderers in `render/`; web service in `service/`.

## Rules

- **`SECRET_KEY` is non-negotiable.** Without it the service refuses to store tokens —
  never add a plaintext fallback.
- **No I/O inside the reconstruction core** (`reconstruct.py`, `activity.py`,
  `periods.py`, `timeutil.py`, `model.py`).
- **Releases are org-managed** via `MagmaMoose/admin`.
- Python ≥ 3.11, pytest + Ruff, `src/` layout, `tests/` mirrors modules. MIT.

## Context files

- `./PROJECT_INDEX.json` — locating unfamiliar code.
- `.claude/COMMON_MISTAKES.md` — always-applicable footguns.
- `./docs/` — published HTTP API and pushed-ingest reference.
