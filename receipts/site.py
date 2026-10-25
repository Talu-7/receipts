"""Static replay site for GitHub Pages: python -m receipts site

Reads receipts (out/receipt-*.json) and the benchmark (out/bench.json) and writes docs/ :
  docs/index.html   animated replay of the recorded runs + benchmark table
  docs/receipts/    each full receipt (html + json)
The page runs no code and holds no keys, so it is safe to host publicly and stays alive after the deadline.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "static" / "site_template.html"


def _public(r: dict) -> dict:
    """The receipt without raw logs (they stay in the full receipt page)."""
    keep = ("receipt_id", "synthetic_demo", "paper", "claim", "repo", "evidence", "verdict", "diagnosis", "seeds_requested", "created_at")
    pub = {k: r.get(k) for k in keep}
    pub["paper"] = {k: (r.get("paper") or {}).get(k) for k in ("title", "url", "arxiv_id")}
    pub["runs"] = [{k: x.get(k) for k in ("seed", "ok", "value", "extras", "seconds", "error", "log_summary")} for x in r.get("runs", [])]
    pub["script"] = {"code": (r.get("script") or {}).get("code", "")}
    infra = r.get("infra") or {}
    pub["infra"] = {k: infra.get(k) for k in ("backend", "wall_seconds", "job_seconds", "models")}
    return pub


def build(out_dir: str = "out", site_dir: str = "docs", repo_url: str = "") -> dict:
    out, site = Path(out_dir), Path(site_dir)
    receipts = []
    for p in sorted(out.glob("receipt-*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if r.get("verdict") and r.get("claim"):
            receipts.append((p, r))
    if not receipts:
        raise SystemExit(f"no receipts found in {out}/ - run `python -m receipts bench bench/planted.json` first")

    # newest receipt wins when the same case was run several times
    latest: dict[str, tuple[Path, dict]] = {}
    for p, r in receipts:
        key = r.get("input") or r["receipt_id"]
        if key not in latest or r["created_at"] >= latest[key][1]["created_at"]:
            latest[key] = (p, r)
    chosen = list(latest.values())

    (site / "receipts").mkdir(parents=True, exist_ok=True)
    for old in (site / "receipts").glob("receipt-*"):
        old.unlink()
    for p, r in chosen:
        shutil.copy(p, site / "receipts" / p.name)
        h = p.with_suffix(".html")
        if h.exists():
            shutil.copy(h, site / "receipts" / h.name)

    bench = None
    bj = out / "bench.json"
    if bj.exists():
        try:
            bench = json.loads(bj.read_text(encoding="utf-8"))
        except ValueError:
            bench = None
    note = None
    if bench:
        used_llm = any(((r.get("diagnosis") or {}).get("source") == "nemotron-ultra") for _, r in chosen)
        note = ("Cause attribution in this run was written by Nemotron Ultra." if used_llm else
                "Verdicts come from deterministic statistics. Cause attribution in this recorded run used the built-in heuristic; "
                "re-run with a Token Factory key to have Nemotron Ultra write it.")

    data = {"receipts": [_public(r) for _, r in chosen], "bench": bench, "bench_note": note, "repo": repo_url,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__RECEIPTS_DATA__", payload)
    (site / "index.html").write_text(html, encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    return {"receipts": len(chosen), "site": str(site), "bench": bool(bench)}
