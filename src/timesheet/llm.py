"""Optional summary callable backed by any OpenAI-compatible endpoint.

`make_llm(cfg)` returns a `(prompt: str) -> str` callable, or None when AI is not
configured (`llm_enabled` is False). It talks to the endpoint with stdlib
`urllib` only — no `openai`/`httpx` dependency, matching the collectors.

The whole layer is best-effort: `summarize.summarize_block` calls this inside a
try/except and falls back to the deterministic summariser on any failure, so a slow
or unreachable endpoint never breaks a reconstruction. Only wire it into the
background refresh (run.py), never a web request — a model can take 10-20s a call.

The system prompt comes from the config's locale, so the label it writes is in
the language the sheet is read in.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .config import Config
from .net import check as net_check
from .net import open_url

log = logging.getLogger("timesheet.llm")

def make_llm(cfg: Config):
    """A prompt->text callable for the configured model, or None if AI is disabled."""
    if not cfg.llm_enabled:
        return None
    system = cfg.strings.llm_system
    # The key is sent as a bearer token on every call; over plain http that is a
    # credential on the wire. `net.check` allows loopback and nothing else.
    base = net_check(cfg.llm_base_url.rstrip("/"))
    url = f"{base}/v1/chat/completions"
    key, model, timeout = cfg.llm_api_key, cfg.llm_model, cfg.llm_timeout
    effort = cfg.llm_reasoning_effort

    def _call(prompt: str) -> str:
        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if effort:  # reasoning models: "low" keeps a one-line label fast and cheap
            payload["reasoning_effort"] = effort
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers={  # noqa: S310 — opened through net.open_url, which enforces https
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })
        with open_url(req, timeout=timeout) as r:
            data = json.load(r)
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text.splitlines()[0].strip() if text else ""

    return _call
