"""The single AI-optional seam.

`summarize_block` turns a cluster of commits into one task line. It is rule-based
by default and needs no AI. Pass an `llm` callable (prompt:str) -> str — e.g.
DeepSeek via a LiteLLM proxy — to get polished Dutch labels; any failure falls
straight back to the deterministic version, so the tool always works offline."""
from __future__ import annotations

import re

from .config import Config
from .model import Commit


def _clean_subject(msg: str) -> str:
    """Strip a conventional-commit prefix: 'feat(scope): thing' -> 'thing'."""
    m = re.match(r"^\w+(?:\([^)]*\))?!?:\s*(.+)$", msg)
    return (m.group(1) if m else msg).strip()


def _wb_trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rstrip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n // 2 else cut).rstrip(" ,;") + "…"


def classify_focus(commits: list[Commit], cfg: Config) -> str:
    kinds = {c.kind for c in commits}
    if kinds == {"review"}:
        return cfg.review_project
    if kinds == {"pr"}:
        return cfg.pr_project
    if kinds == {"issue"}:
        return cfg.issue_project
    blob = " ".join(c.message.lower() for c in commits)
    for project, kws in cfg.focus_rules:
        if any(k in blob for k in kws):
            return project
    return cfg.focus_default_project


def summarize_block(commits: list[Commit], project: str, cfg: Config, llm=None) -> str:
    subjects = [c.message for c in commits]
    if llm is not None:
        prompt = (
            "Vat dit werk samen in één korte, professionele Nederlandse taakregel "
            "(max 10 woorden), zonder commit-jargon of issue-nummers:\n"
            + "\n".join(f"- {s}" for s in subjects)
        )
        try:
            raw = llm(prompt).strip()
            out = raw.splitlines()[0].strip().strip('"').strip() if raw else ""
            if out:
                return _wb_trunc(out, 90)
        except Exception:  # noqa: BLE001, S110 — any LLM failure must fall back, never raise
            pass  # deterministic fallback below

    # Deterministic: one clean, complete phrase — the most descriptive commit
    # subject of the session. Concatenating several subjects and hard-truncating
    # produced unreadable lines like "GHE auth ...; expose acc…"; a single tidy
    # phrase reads far better on the sheet, and the day's breadth already shows
    # through the separate blocks.
    seen: set[str] = set()
    picks: list[str] = []
    for s in subjects:
        c = re.sub(r"\s*\(#\d+\)\s*$", "", _clean_subject(s)).strip()   # drop trailing (#123)
        key = c[:28].lower()
        if key and key not in seen:
            seen.add(key)
            picks.append(c)
    if not picks:
        return project
    headline = max(picks, key=len)                     # most descriptive subject wins
    return _wb_trunc(headline, 80)
