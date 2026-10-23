"""Nemotron Nano: summarise job logs (with a plain-text fallback when no key is set)."""
from __future__ import annotations

from .llm import LLM

SYSTEM = ("You summarise logs from a sandboxed ML reproduction job for a reviewer. Be factual and brief "
          "(max 3 sentences). Say what ran, the final number if any, and the root cause of any failure. "
          "Do not speculate.")


def _fallback(logs: str, ok: bool) -> str:
    lines = [l for l in logs.splitlines() if l.strip() and not l.startswith("RECEIPTS_META")]
    if ok:
        res = [l for l in lines if l.startswith("RECEIPTS_RESULT")]
        return f"Completed. {res[-1][:200] if res else ''}".strip()
    tail = [l for l in lines if "Error" in l or "error" in l or l.startswith("RECEIPTS_ERROR")]
    return "Failed: " + (tail[-1][:240] if tail else (lines[-1][:240] if lines else "no output"))


def summarize_logs(llm: LLM, logs: str, ok: bool) -> str:
    if not llm.available:
        return _fallback(logs, ok)
    tail = logs[-7000:]
    try:
        return llm.chat("nano", SYSTEM, f"Job {'succeeded' if ok else 'FAILED'}. Log tail:\n{tail}", max_tokens=300, temperature=0)
    except Exception:
        return _fallback(logs, ok)
