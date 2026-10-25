"""Nemotron clients on Nebius Token Factory (OpenAI-compatible chat API).

Roles:
  nano  - cheap and fast: summarises job logs
  super - claim extraction + reproduction code
  ultra - deep reasoning about *why* a number differs
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import http
from .config import Settings

# model-id substring (lowercase) -> role; used to auto-resolve exact IDs from /v1/models
_ROLE_MATCH = {
    "nano": ("nemotron-3-nano", ("omni",)),
    "super": ("nemotron-3-super", ()),
    "ultra": ("nemotron-3-ultra", ()),
}


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    # unterminated think block (model hit max_tokens while reasoning)
    return re.sub(r"<think>.*\Z", "", text, flags=re.S).strip()


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles fences + chatter)."""
    text = strip_think(text)
    cands = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):
        cands.append(m.group(1))
    cands.append(text)
    dec = json.JSONDecoder()
    for cand in cands:
        for i, ch in enumerate(cand):
            if ch in "{[":
                try:
                    obj, _ = dec.raw_decode(cand[i:])
                    return obj
                except ValueError:
                    continue
    raise ValueError(f"no JSON found in model output: {text[:200]!r}")


class LLM:
    def __init__(self, settings: Settings):
        self.s = settings
        self._resolved: dict[str, str] = {}
        self._lock = threading.Lock()
        self.usage = {r: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0} for r in _ROLE_MATCH}

    @property
    def available(self) -> bool:
        return bool(self.s.nebius_api_key)

    def _headers(self):
        return {"Authorization": f"Bearer {self.s.nebius_api_key}"}

    def model_for(self, role: str) -> str:
        with self._lock:
            if role in self._resolved:
                return self._resolved[role]
            fallback = {"nano": self.s.nano_model, "super": self.s.super_model, "ultra": self.s.ultra_model}[role]
            chosen = fallback
            try:
                data = http.get_json(self.s.llm_base_url.rstrip("/") + "/models", headers=self._headers(), timeout=20)
                ids = [m["id"] for m in data.get("data", [])]
                needle, excl = _ROLE_MATCH[role]
                hits = [i for i in ids if needle in i.lower() and not any(x in i.lower() for x in excl)]
                if fallback in ids:
                    chosen = fallback
                elif hits:
                    chosen = sorted(hits)[0]
            except Exception:
                pass  # keep fallback; the chat call will surface a real error if it is wrong
            self._resolved[role] = chosen
            return chosen

    def chat(self, role: str, system: str, user: str, *, max_tokens: int = 4096,
             temperature: float = 0.2, retries: int = 5) -> str:
        if not self.available:
            raise RuntimeError("NEBIUS_API_KEY is not set")
        body = {
            "model": self.model_for(role),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # Nemotron 3 models reason before answering; give them headroom on top of the answer budget.
            "max_tokens": max_tokens + int(os.environ.get("RECEIPTS_REASONING_BUDGET", "3000")),
            "temperature": temperature,
        }
        url = self.s.llm_base_url.rstrip("/") + "/chat/completions"
        last: Exception | None = None
        grown = False
        for attempt in range(retries):
            try:
                resp = http.post_json(url, body, headers=self._headers(), timeout=180)
                u = resp.get("usage") or {}
                with self._lock:
                    self.usage[role]["calls"] += 1
                    self.usage[role]["prompt_tokens"] += u.get("prompt_tokens", 0)
                    self.usage[role]["completion_tokens"] += u.get("completion_tokens", 0)
                choice = resp["choices"][0]
                text = strip_think(choice["message"].get("content") or "")
                if text:
                    return text
                if choice.get("finish_reason") == "length" and not grown:
                    grown = True  # all tokens went on reasoning: retry once with double the budget
                    body["max_tokens"] *= 2
                    continue
                raise RuntimeError("model returned an empty answer (token budget spent on reasoning); "
                                   "raise RECEIPTS_REASONING_BUDGET")
            except http.HTTPError as e:
                last = e
                if e.status in (400, 401, 403, 404):
                    break
                # rate limited / overloaded (Token Factory sends 529): back off for longer
                time.sleep(min(60, 5 * 3 ** attempt) if e.status in (429, 503, 529) else 2 ** attempt)
            except Exception as e:  # network hiccup
                last = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM call failed ({role}): {last}")

    def chat_json(self, role: str, system: str, user: str, **kw):
        raw = self.chat(role, system, user, **kw)
        try:
            return extract_json(raw)
        except ValueError:
            fixed = self.chat(role, "Return ONLY valid JSON. No prose, no code fences.",
                              f"Convert this to valid JSON, preserving all content:\n\n{raw}", max_tokens=kw.get("max_tokens", 4096), temperature=0)
            return extract_json(fixed)
