"""The Power Automate ingest path: pushed calendar/email/Teams + server-side
commits -> reconstructed week, and the HTTP surface (auth, view, download)."""
import json
from pathlib import Path

import pytest

from timesheet.config import Config
from timesheet.pipeline import build_from_ingest

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "week_2026-07-20.json").read_text())


def _commits_stub(since, until):
    # Stand in for the live GHE collector: same raw shape the collector emits.
    return FIXTURE["commits"]


def _payload():
    return {
        "week_start": "2026-07-20",
        "meetings": FIXTURE["meetings"],
        # a burst of sent mail Friday afternoon should relabel that admin block
        "emails": [{"sent_utc": "2026-07-24T12:10:00Z", "subject": "re: prd"},
                   {"sent_utc": "2026-07-24T12:25:00Z", "subject": "re: rollout"}],
        "teams": [{"ts_utc": "2026-07-24T12:40:00Z", "chat": "Cloud"}],
    }


def test_build_from_ingest_reconstructs_week():
    monday, days = build_from_ingest(_payload(), Config(), fetch_commits=_commits_stub)
    assert monday.isoformat() == "2026-07-20"
    assert [d.date.weekday() for d in days] == [0, 1, 2, 3, 4]
    total = sum(d.minutes for d in days)
    assert 35 * 60 <= total <= 50 * 60


def test_email_teams_enrichment_relabels_admin():
    _, days = build_from_ingest(_payload(), Config(), fetch_commits=_commits_stub)
    friday = next(d for d in days if d.date.weekday() == 4)
    # the 14:10–14:40 local window (12:10–12:40Z) is an admin block; it should now
    # mention Mail and/or Teams rather than the generic default.
    labels = [b.taak for b in friday.blocks if b.kind == "admin"]
    assert any("Mail" in t or "Teams" in t for t in labels)


def test_http_ingest_requires_token(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "s3cret")
    from fastapi.testclient import TestClient

    import timesheet.collectors.ghe as ghe_mod
    monkeypatch.setattr(ghe_mod, "fetch_commits", _commits_stub)

    # import after env is set so the app/store pick a temp data dir
    import importlib

    from timesheet.service import main
    importlib.reload(main)
    client = TestClient(main.app)

    assert client.post("/api/ingest", json=_payload()).status_code == 401
    assert client.post("/api/ingest", json=_payload(),
                       headers={"X-Ingest-Token": "wrong"}).status_code == 401
    ok = client.post("/api/ingest", json=_payload(), headers={"X-Ingest-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["ok"] is True

    page = client.get("/")
    assert page.status_code == 200 and "TOTAAL" in page.text
    dl = client.get("/uren.xlsx")
    assert dl.status_code == 200 and dl.content[:2] == b"PK"

    # /healthz must stay DB-free (regression: it opened a Postgres connection per
    # probe, blew the 1s timeout, and crash-looped the pod). Exactly {"ok": True}.
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/status").json()["week_start"] == "2026-07-20"


@pytest.fixture(autouse=True)
def _tmp_data(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
