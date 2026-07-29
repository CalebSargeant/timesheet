"""The deterministic summariser: one clean, complete phrase per block — never the
old "subject A; subject B; expose acc…" concatenation the manager saw."""
from datetime import UTC, datetime

from timesheet.config import Config
from timesheet.model import Commit
from timesheet.summarize import summarize_block


def _c(msg: str) -> Commit:
    return Commit(ts=datetime(2026, 7, 1, 10, tzinfo=UTC), repo="r", message=msg)


def test_summary_is_one_clean_complete_phrase():
    cfg = Config()
    commits = [
        _c("feat(ci): GHE auth via release-runner GitHub App to retire PATs"),
        _c("ci: expose acc environment to the release runner (#123)"),
    ]
    out = summarize_block(commits, "CI/CD", cfg)
    assert out == "GHE auth via release-runner GitHub App to retire PATs"
    assert ";" not in out          # not a concatenation of several subjects
    assert not out.endswith("…")   # a complete phrase, not truncated mid-thought


def test_summary_falls_back_to_project_when_empty():
    cfg = Config()
    assert summarize_block([_c("(#7)")], "Development", cfg) == "Development"


def test_llm_output_is_used_when_provided():
    cfg = Config()
    out = summarize_block([_c("fix: thing")], "Development", cfg,
                          llm=lambda _p: '"GHE-auth naar GitHub App"\n(details)')
    assert out == "GHE-auth naar GitHub App"   # quotes + trailing line stripped
