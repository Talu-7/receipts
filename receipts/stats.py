"""Statistical verdict: compare a claimed number against a distribution of reproduced runs."""
from __future__ import annotations

import math

REPRODUCED = "reproduced"
PARTIAL = "partially_reproduced"
DISCREPANCY = "discrepancy_found"
INCONCLUSIVE = "inconclusive"

_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
        10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 30: 2.042}


def t_crit(df: int) -> float:
    if df < 1:
        return float("nan")
    keys = [k for k in _T95 if k <= df]
    return _T95[max(keys)] if df < 30 else 1.96


def summarize(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None, "ci95": None}
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    half = t_crit(n - 1) * std / math.sqrt(n) if n > 1 else float("nan")
    return {"n": n, "mean": mean, "std": std, "min": min(values), "max": max(values),
            "ci95": [mean - half, mean + half] if n > 1 else None}


def decide(claimed: float, values: list[float], requested: int, *, tol: float | None = None,
           higher_is_better: bool = True) -> dict:
    """Return verdict + the numbers behind it.

    `tol` is an absolute tolerance in the claim's units (default: max(1.0, 1% of the claim)).
    A run counts as reproduced if our mean is no worse than the claim by more than
    max(tol, 2 * run-to-run std); partially reproduced up to max(3 * tol, 4 * std); else a discrepancy.
    Fewer than half the requested runs succeeding is inconclusive: we do not accuse a paper
    because our environment broke.
    """
    s = summarize(values)
    need = max(1, math.ceil(requested / 2))
    if s["n"] < need:
        return {"verdict": INCONCLUSIVE, "claimed": claimed, "stats": s, "tolerance": tol,
                "reason": f"only {s['n']} of {requested} runs produced a number (need at least {need})",
                "claimed_in_range": None, "shortfall": None}
    if tol is None:
        tol = max(1.0, 0.01 * abs(claimed))
    std = s["std"] or 0.0
    shortfall = (claimed - s["mean"]) if higher_is_better else (s["mean"] - claimed)  # >0: worse than claimed
    allowed = max(tol, 2 * std)
    partial_allowed = max(3 * tol, 4 * std)
    in_range = s["min"] <= claimed <= s["max"]
    note = ""
    if shortfall <= allowed:
        verdict = REPRODUCED
        if -shortfall > allowed:
            note = "reproduced result is better than the claim"
    elif shortfall <= partial_allowed:
        verdict = PARTIAL
    else:
        verdict = DISCREPANCY
    return {"verdict": verdict, "claimed": claimed, "stats": s, "tolerance": tol, "allowed_gap": allowed,
            "partial_gap": partial_allowed, "shortfall": shortfall, "claimed_in_range": in_range,
            "reason": note, "higher_is_better": higher_is_better}
