"""Nemotron Ultra reasons about WHY a reproduced number differs from the claim."""
from __future__ import annotations

import json

from .llm import LLM

CATEGORIES = [
    "seed_variance", "data_leakage", "dataset_version_mismatch", "hyperparameter_mismatch",
    "metric_definition_difference", "unreported_preprocessing", "code_paper_mismatch",
    "environment_or_dependency", "truncated_or_reduced_compute", "data_unavailable", "cannot_determine",
]

SYSTEM = """You are a careful reproducibility reviewer. Given a claimed result, the distribution of numbers
obtained by re-running the paper's code, run logs, extra variants the script computed, and any web
evidence (errata, replication attempts), explain the most likely reason for the gap.
Rules:
- Choose exactly one category from the list. Use "cannot_determine" if the evidence does not support one.
- Ground every statement in the supplied numbers, code or evidence; quote the numbers.
- If a computed variant's value matches the claim, say so: that is strong evidence.
- Never accuse the authors of misconduct. Describe a discrepancy under stated conditions.
- Suggest one concrete check that would confirm or refute your explanation.
Reply with JSON only:
{"category": "...", "explanation": "<3-6 sentences>", "evidence": ["<bullet>", ...], "confidence": <0..1>, "suggested_check": "<one sentence>"}"""


def _variant_match(claim: dict, per_seed: list[dict], tol: float) -> tuple[str, float] | None:
    keys = {k for r in per_seed for k, v in (r.get("extras") or {}).items() if isinstance(v, (int, float))}
    best = None
    for k in keys:
        vals = [r["extras"][k] for r in per_seed if isinstance((r.get("extras") or {}).get(k), (int, float))]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        gap = abs(mean - claim["value"])
        if gap <= tol and (best is None or gap < best[2]):
            best = (k, mean, gap)
    return (best[0], best[1]) if best else None


def offline_diagnosis(claim: dict, verdict: dict, per_seed: list[dict], summaries: list[str]) -> dict:
    tol = max(1.0, verdict.get("tolerance") or 1.0)
    if verdict.get("reduced_scale"):
        return {"category": "truncated_or_reduced_compute",
                "explanation": verdict.get("reason", "The run was shrunk and is not comparable to the claim."),
                "evidence": [f"deviations: {verdict.get('deviations', '')}"], "confidence": 0.8,
                "suggested_check": "Re-run at the paper's full scale on adequate hardware.", "source": "offline-heuristic"}
    if verdict["verdict"] == "inconclusive":
        txt = summaries[0] if summaries else "The reproduction could not be run."
        cat = "data_unavailable" if "No such file" in txt or "FileNotFound" in txt else "environment_or_dependency"
        return {"category": cat, "explanation": f"Not a discrepancy: the runs did not produce numbers. {txt}",
                "evidence": summaries[:2], "confidence": 0.5, "suggested_check": "Obtain the missing data/dependency and re-run.",
                "source": "offline-heuristic"}
    m = _variant_match(claim, per_seed, tol)
    mean = verdict["stats"]["mean"]
    if m:
        k, v = m
        low = k.lower()
        if any(t in low for t in ("all_data", "leak", "tuned_on_test", "no_dedup", "duplicate")):
            cat = "data_leakage"
        elif "train_set" in low or "metric" in low:
            cat = "metric_definition_difference"
        else:
            cat = "unreported_preprocessing"
        return {"category": cat,
                "explanation": (f"The claimed {claim['value']:.1f} is not reached by the protocol as described "
                                f"(mean {mean:.1f}), but the variant '{k}' averages {v:.1f}, which matches the claim."),
                "evidence": [f"described protocol mean = {mean:.2f}", f"{k} mean = {v:.2f}", f"claimed = {claim['value']:.2f}"],
                "confidence": 0.6, "suggested_check": f"Inspect the code path that corresponds to '{k}' and confirm it was used for the reported number.",
                "source": "offline-heuristic"}
    return {"category": "cannot_determine", "explanation": "No computed variant explains the gap; set NEBIUS_API_KEY to enable Nemotron Ultra's analysis.",
            "evidence": [], "confidence": 0.2, "suggested_check": "Re-run with more seeds and compare hyperparameters with the paper's appendix.",
            "source": "offline-heuristic"}


def diagnose(llm: LLM, claim: dict, verdict: dict, per_seed: list[dict], summaries: list[str],
             script: str, evidence: list[dict]) -> dict:
    if not llm.available:
        return offline_diagnosis(claim, verdict, per_seed, summaries)
    payload = {
        "claim": claim,
        "verdict": {k: verdict.get(k) for k in ("verdict", "stats", "tolerance", "shortfall", "claimed_in_range", "reason")},
        "per_seed": [{"seed": r["seed"], "ok": r["ok"], "value": r["value"], "extras": r.get("extras"), "error": r.get("error")} for r in per_seed],
        "log_summaries": summaries[:4],
        "web_evidence": evidence[:8],
        "categories": CATEGORIES,
    }
    user = json.dumps(payload, indent=1, default=str)[:24_000] + f"\n\nReproduction script:\n```python\n{script[:7000]}\n```"
    try:
        out = llm.chat_json("ultra", SYSTEM, user, max_tokens=2500, temperature=0.2)
    except Exception as e:
        d = offline_diagnosis(claim, verdict, per_seed, summaries)
        d["explanation"] += f" (Ultra call failed: {str(e)[:120]})"
        return d
    if isinstance(out, list):  # models sometimes wrap the object in a list
        out = next((x for x in out if isinstance(x, dict)), None)
    if not isinstance(out, dict):
        d = offline_diagnosis(claim, verdict, per_seed, summaries)
        d["explanation"] += " (Ultra returned no usable diagnosis)"
        return d
    if out.get("category") not in CATEGORIES:
        out["category"] = "cannot_determine"
    out["source"] = "nemotron-ultra"
    return out
