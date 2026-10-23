"""Nemotron Super: extract the paper's quantitative claims and pick the headline one."""
from __future__ import annotations

from .llm import LLM

SYSTEM = """You extract quantitative, checkable claims from machine-learning / science papers.
Reply with JSON only. Never invent numbers: every value must appear in the supplied text."""

PROMPT = """Paper: {title}

Abstract:
{abstract}

Full text (may be truncated; tables are flattened with ' | '):
{text}

Task: list the paper's main quantitative claims (max 6) that could in principle be re-run.
Return JSON:
{{
  "claims": [
    {{"id": "c1",
      "statement": "<one sentence>",
      "metric": "<metric name>",
      "value": <number, in the same units as the paper>,
      "unit": "<% or other>",
      "dataset": "<dataset / benchmark>",
      "higher_is_better": true|false,
      "where": "<Table/Figure/section>",
      "tractable": true|false,   // re-runnable in < 30 minutes on one GPU using public code/data/checkpoints
      "est_gpu_minutes": <number>}}
  ],
  "headline": "<id of the paper's headline claim, preferring one that is tractable>"
}}"""


def extract_claims(llm: LLM, meta: dict, fulltext: str) -> dict:
    text = (fulltext or "")[:45_000] or "(full text unavailable; use the abstract only)"
    out = llm.chat_json("super", SYSTEM, PROMPT.format(title=meta["title"], abstract=meta["abstract"], text=text),
                        max_tokens=3000, temperature=0.1)
    claims = []
    for i, c in enumerate(out.get("claims", []), 1):
        try:
            c["value"] = float(c["value"])
        except (TypeError, ValueError, KeyError):
            continue
        c.setdefault("id", f"c{i}")
        c.setdefault("higher_is_better", True)
        c.setdefault("tractable", True)
        claims.append(c)
    if not claims:
        raise RuntimeError("no checkable numeric claim found in the paper")
    headline = next((c for c in claims if c["id"] == out.get("headline")), None)
    if headline is None or not headline.get("tractable", True):
        headline = next((c for c in claims if c.get("tractable", True)), claims[0])
    return {"claims": claims, "headline": headline}
