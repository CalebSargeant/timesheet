# timesheet

Reconstructs a defensible working week from calendar, mail, chat, and GitHub activity.
Renders it as a live HTML page and an emailed `.xlsx`. Optional AI only polishes labels —
never runs inside a web request.

@.claude/COMMON_MISTAKES.md

Before locating unfamiliar code, read `./PROJECT_INDEX.json` first.

## Commands

```bash
pip install -e ".[dev]"
pytest -q                          # full suite
ruff check .                       # lint
ruff format .                      # format (CI runs --check)

# Offline demo (no network, no account)
python -m timesheet.demo tests/fixtures/week_2026-07-20.json out/

# Service
export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export PUBLIC_URL=http://localhost:8000
export GITHUB_OAUTH_CLIENT_ID=...
export GITHUB_OAUTH_CLIENT_SECRET=...
uvicorn timesheet.service.main:app --reload

# Scheduled refresh / delivery
python -m timesheet.run            # all accounts, no delivery
python -m timesheet.run --send     # all accounts + deliver
python -m timesheet.run --user alice
```

## Architecture

`reconstruct.py` is the pure core: fixed calendar anchors → correspondence sittings →
day-length estimate → free-time fill. No I/O. Everything else feeds it or presents its
output. Entry points: `pipeline.py:collect` (live pulls), `pipeline.py:build_week`
(pure, testable offline), `run.py` (CLI), `service/main.py` (web).

`RECONSTRUCT_VERSION` in `config.py` gates the week cache: a version bump recomputes
stale stored weeks automatically on the next read.

## Rules

- **`SECRET_KEY` is non-negotiable.** Without it the service refuses to store tokens at
  all — never add a plaintext fallback.
- **Releases are org-managed.** `release.yml` is caldrith's — tune from `MagmaMoose/admin`.
- **No I/O inside the reconstruction core** (`reconstruct.py`, `activity.py`,
  `periods.py`, `timeutil.py`, `model.py`). Collectors and the web layer are the edges.
- Python ≥ 3.11, **pytest + Ruff**, `src/` layout, `tests/` mirrors modules. MIT.

## [tooling]

- Line-range reads over whole files; `PROJECT_INDEX.json` to locate code first.
- Pipe noisy output through `head`/`grep` or redirect to `.claude/last_output.txt`.

## [maintenance]

- Bug that cost >1h → `.claude/COMMON_MISTAKES.md`.
- Public behaviour / env var / API changed → `README.md` and `docs/`.
- `PROJECT_INDEX.json` stale after a new module or refactor: regenerate that section,
  bump `generated`.
- Keep this file under ~500 tokens. Push detail into on-demand `.claude/` files.

This file is canonical. `AGENTS.md` restates it — edit both together.
