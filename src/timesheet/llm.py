"""Optional summary callable backed by the house LiteLLM proxy.

`make_llm(cfg)` returns a `(prompt: str) -> str` callable, or None when AI is not
configured (`llm_enabled` is False). It talks to the OpenAI-compatible proxy with
stdlib `urllib` only — no `openai`/`httpx` dependency, matching the collectors.

The whole layer is best-effort: `summarize.summarize_block` calls this inside a
try/except and falls back to the deterministic summariser on any failure, so a slow
or unreachable proxy never breaks a reconstruction. Only wire it into the background
refresh (run.py), never a web request — the proxy's models can take 10-20s a call.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .config import Config

log = logging.getLogger("timesheet.llm")

_SYSTEM = (
    "Je vat technisch werk samen tot één korte, professionele Nederlandse taakregel "
    "voor een urenstaat. Maximaal 10 woorden. Geen commit-jargon, geen issue-nummers, "
    "geen aanhalingstekens. Antwoord met alleen de taakregel."
)


def make_llm(cfg: Config):
    """A prompt->text callable for the LiteLLM proxy, or None if AI is disabled."""
    if not cfg.llm_enabled:
        return None
    base = cfg.llm_base_url.rstrip("/")
    url = f"{base}/v1/chat/completions"
    key, model, timeout = cfg.llm_api_key, cfg.llm_model, cfg.llm_timeout

    def _call(prompt: str) -> str:
        body = json.dumps({
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text.splitlines()[0].strip() if text else ""

    return _call
