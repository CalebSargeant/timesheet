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
            out = llm(prompt).strip().strip('"')
            if out:
                return _wb_trunc(out, 90)
        except Exception:  # noqa: BLE001, S110 — any LLM failure must fall back, never raise
            pass  # deterministic fallback below
    seen: set[str] = set()
    picks: list[str] = []
    for s in subjects:
        c = _clean_subject(s)
        c = re.sub(r"\s*\(#\d+\)\s*$", "", c)          # drop trailing (#123)
        key = c[:24].lower()
        if key and key not in seen:
            seen.add(key)
            picks.append(c)
    label = "; ".join(picks[:3])
    return _wb_trunc(label, 70) if label else project
