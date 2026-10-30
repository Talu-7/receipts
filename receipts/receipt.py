"""The receipt: a self-contained JSON + HTML record of one reproduction attempt."""
from __future__ import annotations

import hashlib
import math
import json
from html import escape
from pathlib import Path

LABELS = {
    "reproduced": ("REPRODUCED", "#1f9d55"),
    "partially_reproduced": ("PARTIALLY REPRODUCED", "#d9a400"),
    "discrepancy_found": ("DISCREPANCY FOUND", "#d6336c"),
    "inconclusive": ("INCONCLUSIVE", "#6b7280"),
}


def canonical_hash(receipt: dict) -> str:
    body = {k: v for k, v in receipt.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _fmt(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def _digits(ref) -> int:
    """Decimals that suit the metric: 94.0 for percentages, 0.3721 for an MSE of 0.37."""
    a = abs(ref or 0) or 1.0
    return 1 if a >= 10 else min(4, max(2, 3 - math.floor(math.log10(a))))


def _fmtv(x, ref=None):
    return _fmt(x, _digits(x if ref is None else ref))


def _gauge(claimed: float, stats: dict) -> str:
    if not stats or stats.get("n", 0) == 0:
        return ""
    lo, hi = min(claimed, stats["min"]), max(claimed, stats["max"])
    pad = max((hi - lo) * 0.6, abs(claimed) * 0.01, 1e-6)  # relative to the claim's scale
    lo, hi = lo - pad, hi + pad
    W = 720
    x = lambda v: 20 + (v - lo) / (hi - lo) * (W - 40)
    ci = stats.get("ci95")
    parts = [f'<svg viewBox="0 0 {W} 90" width="100%" role="img" aria-label="claimed vs reproduced">',
             f'<line x1="20" y1="55" x2="{W-20}" y2="55" stroke="#4b5563" stroke-width="2"/>',
             f'<rect x="{x(stats["min"]):.1f}" y="42" width="{max(x(stats["max"])-x(stats["min"]),3):.1f}" height="26" rx="4" fill="#3b82f6" opacity=".35"/>']
    if ci:
        parts.append(f'<rect x="{x(ci[0]):.1f}" y="46" width="{max(x(ci[1])-x(ci[0]),3):.1f}" height="18" rx="3" fill="#3b82f6" opacity=".8"/>')
    parts += [f'<line x1="{x(stats["mean"]):.1f}" y1="38" x2="{x(stats["mean"]):.1f}" y2="72" stroke="#fff" stroke-width="3"/>',
              f'<text x="{x(stats["mean"]):.1f}" y="88" fill="#e5e7eb" font-size="13" text-anchor="middle">reproduced mean {_fmtv(stats["mean"], claimed)}</text>',
              f'<line x1="{x(claimed):.1f}" y1="14" x2="{x(claimed):.1f}" y2="72" stroke="#f43f5e" stroke-width="3" stroke-dasharray="5 3"/>',
              f'<text x="{x(claimed):.1f}" y="11" fill="#fda4af" font-size="13" text-anchor="middle">claimed {_fmtv(claimed)}</text>',
              "</svg>"]
    return "".join(parts)


def render_html(r: dict) -> str:
    v, claim, st = r["verdict"], r["claim"], r["verdict"]["stats"]
    label, color = LABELS.get(v["verdict"], (v["verdict"].upper(), "#6b7280"))
    d = r.get("diagnosis") or {}
    infra = r["infra"]
    synthetic = ('<div class="synthetic">SYNTHETIC DEMO CASE: this is a bundled example, not a real paper, '
                 'and no real authors are implicated.</div>') if r.get("synthetic_demo") else ""
    rows = "".join(
        f'<tr><td>{x["seed"]}</td><td>{"ok" if x["ok"] else "failed"}</td><td>{_fmtv(x["value"], claim["value"])}</td>'
        f'<td>{escape(json.dumps(x.get("extras") or {}))}</td><td>{x["seconds"]:.1f}s</td>'
        f'<td>{escape(x.get("job_id") or "")}</td><td>{escape(x.get("log_summary") or x.get("error") or "")}</td></tr>'
        for x in r["runs"])
    ev = "".join(f'<li><b>{escape(e["kind"])}</b>: <a href="{escape(e["url"])}">{escape(e["title"] or e["url"])}</a>'
                 f' <span class="muted">{escape(e.get("why") or e.get("snippet", "")[:160])}</span></li>' for e in r.get("evidence", [])) or \
        '<li class="muted">No errata or replication reports found for this paper (or search not configured).</li>'
    why_head = "Why this is inconclusive" if v["verdict"] == "inconclusive" else "Why does it differ?"
    dbul = "".join(f"<li>{escape(str(b))}</li>" for b in d.get("evidence", []))
    logs = "".join(f'<details><summary>seed {x["seed"]} log</summary><pre>{escape((x.get("logs") or "")[-5000:])}</pre></details>' for x in r["runs"])
    repo = r.get("repo") or {}
    c = claim["value"]
    cmp_line = (f'claimed <b>{_fmtv(c)}</b> vs reproduced <b>{_fmtv(st["mean"], c)}</b> '
                f'(±{_fmtv(st["std"], c)} over {st["n"]} of {r["seeds_requested"]} runs)') if st["n"] else \
        f'claimed <b>{_fmtv(c)}</b>; no run produced a number'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Receipt {escape(r["receipt_id"])}</title><style>
body{{background:#0b0f17;color:#e5e7eb;font:15px/1.55 system-ui,sans-serif;margin:0}}
main{{max-width:920px;margin:0 auto;padding:28px 18px 80px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em}}
.banner{{border-radius:12px;padding:18px 20px;margin:18px 0;background:{color}22;border:1px solid {color}}}
.banner .v{{font-size:26px;font-weight:800;color:{color};letter-spacing:.02em}}
.muted{{color:#9ca3af}} a{{color:#93c5fd}} table{{width:100%;border-collapse:collapse;font-size:13px}}
td,th{{border-bottom:1px solid #1f2937;padding:6px 8px;text-align:left;vertical-align:top}}
pre{{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:12px;overflow:auto;font-size:12.5px}}
.synthetic{{background:#78350f;color:#fde68a;padding:10px 14px;border-radius:8px;margin:14px 0;font-weight:600}}
code{{background:#111827;padding:2px 6px;border-radius:4px}} summary{{cursor:pointer;color:#9ca3af}}
</style></head><body><main>
<h1>Receipt <code>{escape(r["receipt_id"])}</code></h1>
<div class="muted">{escape(r["created_at"])} · integrity hash <code>{escape(r["sha256"][:16])}…</code></div>
{synthetic}
<h2>Paper</h2><div><b>{escape(r["paper"]["title"])}</b><br><span class="muted">{escape(r["paper"].get("url") or "")}</span></div>
<h2>Claim under test</h2><div>{escape(claim["statement"])}<br><span class="muted">{escape(str(claim.get("metric")))} · {escape(str(claim.get("dataset")))} · {escape(str(claim.get("where")))}</span></div>
<div class="banner"><div class="v">{label}</div><div>{cmp_line}</div><div class="muted">{escape(v.get("reason") or "")}</div>{_gauge(claim["value"], st)}</div>
<h2>{why_head}</h2>
<div><b>{escape(str(d.get("category", "n/a")).replace("_", " "))}</b> <span class="muted">(confidence {d.get("confidence", "-")}; source: {escape(str(d.get("source", "-")))})</span></div>
<p>{escape(d.get("explanation", "No diagnosis was needed: the claim reproduced." if v["verdict"] == "reproduced" else ""))}</p>
<ul>{dbul}</ul><div class="muted">Suggested check: {escape(d.get("suggested_check", "-"))}</div>
<h2>Per-seed runs</h2><table><tr><th>seed</th><th>status</th><th>value</th><th>extras</th><th>time</th><th>job</th><th>summary</th></tr>{rows}</table>
<h2>Web evidence (Tavily)</h2><ul>{ev}</ul>
<h2>Executed code</h2><div class="muted">sha256 {escape(r["script"]["sha256"])} · requirements: {escape(", ".join(r["script"]["requirements"]) or "none")}</div>
<pre>{escape(r["script"]["code"])}</pre>
<h2>Environment</h2><table>
<tr><td>repo</td><td>{escape(str(repo.get("url")))} @ {escape(str(repo.get("commit")))}</td></tr>
<tr><td>backend / image / platform</td><td>{escape(str(infra["backend"]))} / {escape(str(infra.get("image")))} / {escape(str(infra.get("platform")))}</td></tr>
<tr><td>job-seconds / wall-seconds / est. cost</td><td>{infra["job_seconds"]:.1f} / {infra["wall_seconds"]:.1f} / ${infra["est_cost_usd"]:.4f}</td></tr>
<tr><td>models</td><td>{escape(json.dumps(infra.get("models")))}</td></tr></table>
<h2>Re-run this receipt</h2><pre>python -m receipts verify receipt-{escape(r["receipt_id"])}.json</pre>
<h2>Raw logs</h2>{logs}
</main></body></html>"""


def write(r: dict, out_dir: str) -> tuple[str, str]:
    r["sha256"] = canonical_hash(r)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / f"receipt-{r['receipt_id']}.json"
    hp = d / f"receipt-{r['receipt_id']}.html"
    jp.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    hp.write_text(render_html(r), encoding="utf-8")
    return str(jp), str(hp)
