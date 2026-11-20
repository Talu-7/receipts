"""RECLAIM (arXiv 2609.28850) as an accuracy benchmark for Receipts.

RECLAIM pins one reproduction target per NeurIPS 2025 paper: a configuration, a metric, the reported value and
links to verified code/data. Tiers: Easy (code, data and weights released), Medium (retrain), Hard (reimplement).
Dataset: https://huggingface.co/datasets/Mithilss/reclaim (CC BY 4.0).

Every Easy target was verified by the RECLAIM authors to have its artifacts available, so a correct system should
come out "reproduced" or honestly "inconclusive"; any "discrepancy" is flagged for human review, because it is either a
real finding or a false accusation, and we must know which.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import http

HF = "https://huggingface.co/datasets/Mithilss/reclaim/resolve/main/"
FILES = ("eval_100.jsonl", "dev_split.jsonl")

# metrics where a smaller number is better
_LOWER = re.compile(r"error|loss|perplexity|\bppl\b|\bram\b|memory|latency|distance|\bfad\b|\bfid\b|\bgap\b|mase|"
                    r"\bmse\b|\bmae\b|rmse|chamfer|lower is better|lower =|\(lower|time\b|seconds|wer\b|cer\b", re.I)
_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


def fetch(data_dir: str | Path) -> list[Path]:
    d = Path(data_dir); d.mkdir(parents=True, exist_ok=True)
    out = []
    for name in FILES:
        p = d / name
        if not p.exists():
            p.write_text(http.request(HF + name, timeout=120), encoding="utf-8")
        out.append(p)
    return out


def load(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_value(text: str) -> dict | None:
    """'77.0 +/- 1.8%' -> {value: 77.0, spread: 1.8, unit: '%'}; '8.56 kB' -> {value: 8.56, unit: 'kB'}; None if no number."""
    s = (text or "").replace("±", "+/-").replace("−", "-")
    m = re.search(rf"({_NUM})\s*(?:\+/-|\+-)\s*({_NUM})", s)
    if m:
        value, spread = float(m.group(1)), float(m.group(2))
    else:
        m = re.search(_NUM, s)
        if not m:
            return None
        value, spread = float(m.group(0)), None
    rest = s[m.end():].strip()
    unit = "%" if rest.startswith("%") or s.rstrip().endswith("%") else (re.match(r"[A-Za-z]+", rest).group(0) if re.match(r"[A-Za-z]+", rest) else "")
    return {"value": value, "spread": spread, "unit": unit}


def higher_is_better(metric: str) -> bool:
    return not _LOWER.search(metric or "")


def to_task(row: dict) -> dict | None:
    """A RECLAIM row as a pinned Receipts task, or None when the target is not a single comparable number."""
    mt = row.get("match_target") or {}
    if mt.get("match_bar_kind") != "point_estimate":
        return None
    v = parse_value(str(mt.get("value", "")))
    if not v:
        return None
    links = row.get("verified_links") or {}
    repo = next((u for u in (links.get("code") or []) + (links.get("paper_or_project") or [])
                 if re.match(r"https://github\.com/[^/]+/[^/]+", u or "")), None)
    if repo:
        repo = re.match(r"https://github\.com/[^/]+/[^/#?]+", repo).group(0).removesuffix(".git")
    hours = row.get("audited_h100_hours")
    if hours is None:
        hours = row.get("h100_hours_estimate")
    claim = {
        "statement": row.get("central_claim", ""),
        "metric": mt.get("metric", ""),
        "value": v["value"],
        "unit": v["unit"],
        "dataset": mt.get("scope", ""),
        "where": f"RECLAIM target: {mt.get('config', '')}",
        "higher_is_better": higher_is_better(mt.get("metric", "")),
        "est_gpu_minutes": (hours or 0) * 60,
    }
    return {
        "id": row["custom_id"],
        "input": f"https://arxiv.org/abs/{row['custom_id']}",
        "tier": row.get("tier"),
        "h100_hours": hours,
        "claim": claim,
        # the paper's own reported spread is the honest tolerance when it gives one
        "tolerance": v["spread"],
        "repo": repo,
        "task": "\n".join(filter(None, [row.get("agent_task"), "Target configuration: " + str(mt.get("config", "")),
                                        "Report exactly this metric: " + str(mt.get("metric", ""))])),
    }


def select(rows: list[dict], tiers=("Easy",), max_h100_hours: float = 0.25, limit: int | None = None,
           ids: list[str] | None = None) -> list[dict]:
    tasks = []
    for r in rows:
        if ids and r["custom_id"] not in ids:
            continue
        if not ids and r.get("tier") not in tiers:
            continue
        t = to_task(r)
        if not t or not t["repo"]:
            continue
        if not ids and (t["h100_hours"] is None or t["h100_hours"] > max_h100_hours):
            continue
        tasks.append(t)
    tasks.sort(key=lambda t: t["h100_hours"] or 0)
    return tasks[:limit] if limit else tasks


def summarize(results: list[dict]) -> dict:
    """results: [{id, verdict, cause, mean, claimed, ...}] -> headline accuracy numbers."""
    n = len(results)
    by = lambda v: sum(1 for r in results if r["verdict"] == v)
    definite = by("reproduced") + by("partially_reproduced") + by("discrepancy_found")
    return {
        "papers": n,
        "reproduced": by("reproduced"),
        "partially_reproduced": by("partially_reproduced"),
        "discrepancy_found_needs_review": by("discrepancy_found"),
        "inconclusive": by("inconclusive"),
        "coverage_definite_verdict": f"{definite}/{n}",
        "reproduced_rate": f"{by('reproduced')}/{n}",
    }
