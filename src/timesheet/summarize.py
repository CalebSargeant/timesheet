"""The single AI-optional seam.

`summarize_block` turns a cluster of commits into one task line. It is rule-based
by default and needs no AI. Pass an `llm` callable `(prompt: str) -> str` to get
polished labels in the config's locale; any failure falls straight back to the
deterministic version, so the tool always works offline."""
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
    labels = cfg.labels
    kinds = {c.kind for c in commits}
    if kinds == {"review"}:
        return labels.review_project
    if kinds == {"pr"}:
        return labels.pr_project
    if kinds == {"issue"}:
        return labels.issue_project
    blob = " ".join(c.message.lower() for c in commits)
    for key, kws in cfg.focus_rules:
        if any(k in blob for k in kws):
            return cfg.focus_category(key)
    return labels.focus_project


def _phrases(commits: list[Commit]) -> list[str]:
    """The session's distinct commit subjects, cleaned, most descriptive first."""
    seen: set[str] = set()
    picks: list[str] = []
    for c in commits:
        # drop a conventional-commit prefix and any trailing (#123)
        text = re.sub(r"\s*\(#\d+\)\s*$", "", _clean_subject(c.message)).strip()
        key = text[:28].lower()
        if key and key not in seen:
            seen.add(key)
            picks.append(text)
    return sorted(picks, key=len, reverse=True)


def summarize_lines(commits: list[Commit], project: str, cfg: Config, llm=None,
                    count: int = 1) -> list[str]:
    """Up to `count` distinct task lines for one session.

    A long run on one theme is split across several rows by the focus cap, and
    repeating one line down all of them reads as generated. Every line here is a
    real subject from that same session, so the variety costs nothing in honesty.
    Falls back to repeating the best line when the session has fewer subjects
    than rows.
    """
    best = summarize_block(commits, project, cfg, llm)
    lines = [best]
    for phrase in _phrases(commits):
        if len(lines) >= count:
            break
        candidate = _wb_trunc(phrase, 80)
        if candidate not in lines:
            lines.append(candidate)
    while len(lines) < count:
        lines.append(best)
    return lines[:count]


def summarize_block(commits: list[Commit], project: str, cfg: Config, llm=None) -> str:
    subjects = [c.message for c in commits]
    if llm is not None:
        items = "\n".join(f"- {s}" for s in subjects)
        try:
            raw = llm(cfg.strings.llm_prompt.format(items=items)).strip()
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
    picks = _phrases(commits)
    if not picks:
        return project
    return _wb_trunc(picks[0], 80)                     # most descriptive subject wins
