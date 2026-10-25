"""CLI:  python -m receipts {run,serve,verify,doctor}"""
from __future__ import annotations

import argparse
import json
import sys

from .config import Settings

ICON = {"reproduced": "✅", "partially_reproduced": "🟡", "discrepancy_found": "🚩", "inconclusive": "❔"}


def cmd_run(a) -> int:
    from .pipeline import Pipeline

    def show(e):
        if e["stage"] != "receipt":
            print(f"[{e['stage']:<9}] {e['msg']}", flush=True)

    s = Settings.load()
    if a.out:
        s.out_dir = a.out
    rec = Pipeline(s, emit=show).run(a.input, seeds=a.seeds, runner=a.runner, tolerance=a.tolerance,
                                         claim_hint=a.claim)
    v = rec["verdict"]
    print()
    print(f"{ICON.get(v['verdict'], '')}  {v['verdict'].upper().replace('_', ' ')}")
    if v["stats"]["n"]:
        st = v["stats"]
        print(f"   claimed {rec['claim']['value']:.2f}  vs  reproduced {st['mean']:.2f} ± {st['std']:.2f}  (n={st['n']}, range {st['min']:.2f}-{st['max']:.2f})")
    if rec.get("diagnosis"):
        print(f"   cause: {rec['diagnosis']['category']} - {rec['diagnosis']['explanation']}")
    print(f"   receipt: {rec['_paths']['html']}")
    return 0


def cmd_serve(a) -> int:
    from .server import serve
    serve(a.host, a.port)
    return 0


def cmd_verify(a) -> int:
    """Re-execute a receipt's stored script for its stored seeds and compare with the recorded numbers."""
    from .runner import JobSpec, LocalRunner
    from .receipt import canonical_hash

    rec = json.load(open(a.receipt, encoding="utf-8"))
    if rec.get("sha256") and canonical_hash(rec) != rec["sha256"]:
        print("!! integrity hash mismatch: this receipt file was modified after it was written")
        return 2
    run = LocalRunner()
    repo = rec.get("repo") or {}
    bundled = rec.get("synthetic_demo") or not repo.get("url")
    bad = 0
    print(f"verifying {len(rec['runs'])} runs of receipt {rec['receipt_id']} ...")
    for r in rec["runs"]:
        if not r["ok"]:
            print(f"  seed {r['seed']}: (was a failed run, skipped)")
            continue
        spec = JobSpec(script=rec["script"]["code"], seed=r["seed"], repo_url=repo.get("url"), repo_commit=repo.get("commit"),
                       requirements=rec["script"]["requirements"], install=not bundled, install_repo_requirements=not bundled)
        out = run.run(spec)
        ok = out.ok and abs(out.value - r["value"]) <= a.atol
        bad += 0 if ok else 1
        print(f"  seed {r['seed']}: recorded {r['value']:.3f}  re-run {out.value if out.ok else 'FAILED'}  {'OK' if ok else 'MISMATCH'}")
    print("receipt re-verified" if not bad else f"{bad} runs did not match")
    return 0 if not bad else 1


def cmd_bench(a) -> int:
    """Run a list of papers with known expected verdicts and report agreement, cost and time."""
    from .pipeline import Pipeline
    from pathlib import Path

    items = json.load(open(a.papers, encoding="utf-8"))
    s = Settings.load()
    s.out_dir = a.out or s.out_dir
    rows = []
    for it in items:
        rec = Pipeline(s).run(it["input"], seeds=a.seeds, runner=a.runner)
        v = rec["verdict"]["verdict"]
        exp = it.get("expected")
        rows.append({"input": it["input"], "note": it.get("note", ""), "expected": exp, "got": v,
                     "match": (exp is None or exp == "any" or exp == v),
                     "cause": (rec.get("diagnosis") or {}).get("category"), "expected_cause": it.get("expected_cause"),
                     "wall_s": round(rec["infra"]["wall_seconds"], 1), "job_s": round(rec["infra"]["job_seconds"], 1),
                     "est_cost_usd": round(rec["infra"]["est_cost_usd"], 4), "receipt": rec["_paths"]["html"]})
        print(f"{'OK ' if rows[-1]['match'] else 'BAD'} {it['input'][:60]:<60} expected={exp} got={v}", flush=True)
    attempted = [r for r in rows if r["got"] != "inconclusive"]
    labelled = [r for r in rows if r["expected"] not in (None, "any")]
    md = ["| input | expected | got | match | wall s | job s | est $ |", "|---|---|---|---|---|---|---|"]
    md += [f"| {r['input'][:50]} | {r['expected']} | {r['got']} | {'yes' if r['match'] else '**NO**'} | {r['wall_s']} | {r['job_s']} | {r['est_cost_usd']} |" for r in rows]
    planted = [r for r in rows if r["expected"] == "discrepancy_found"]
    clean = [r for r in rows if r["expected"] == "reproduced"]
    caused = [r for r in rows if r.get("expected_cause")]
    summary = {
        "detection_rate_(planted_errors_flagged)": f"{sum(r['got'] == 'discrepancy_found' for r in planted)}/{len(planted)}" if planted else "n/a",
        "false_accusations_(clean_cases_flagged)": f"{sum(r['got'] in ('discrepancy_found', 'partially_reproduced') for r in clean)}/{len(clean)}" if clean else "n/a",
        "diagnosis_accuracy": f"{sum(r['cause'] in r['expected_cause'] for r in caused)}/{len(caused)}" if caused else "n/a",
        "papers": len(rows),
        "attempted_(not_inconclusive)": len(attempted),
        "agreement_with_labels": f"{sum(r['match'] for r in labelled)}/{len(labelled)}" if labelled else "n/a",
        "false_accusations": sum(1 for r in rows if r["expected"] == "reproduced" and r["got"] == "discrepancy_found"),
        "mean_wall_s": round(sum(r["wall_s"] for r in rows) / max(1, len(rows)), 1),
        "total_est_cost_usd": round(sum(r["est_cost_usd"] for r in rows), 4),
    }
    out = Path(s.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bench.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    (out / "bench.md").write_text("\n".join(md) + "\n\n" + "\n".join(f"- **{k}**: {v}" for k, v in summary.items()) + "\n", encoding="utf-8")
    print("\n" + "\n".join(f"{k}: {v}" for k, v in summary.items()))
    print(f"report: {out / 'bench.md'}")
    return 0


def cmd_site(a) -> int:
    from .site import build
    info = build(a.out, a.dir, a.repo)
    print(f"built {info['site']}/index.html from {info['receipts']} receipts" + (" + benchmark" if info["bench"] else ""))
    print("next: upload the 'docs' folder to GitHub and turn on Pages (see SUBMISSION_GUIDE.md)")
    return 0


def cmd_bundle(a) -> int:
    import os
    from .publish import bundle
    n, problems = bundle(os.getcwd(), a.to)
    print(f"copied {n} files to {a.to}")
    if problems:
        print("NOT SAFE TO PUBLISH:")
        for p in problems:
            print("  -", p)
        return 1
    print("safety check passed: no keys or tokens in that folder.")
    print("Now upload the CONTENTS of that folder to GitHub (see SUBMISSION_GUIDE.md).")
    return 0


def cmd_leakcheck(a) -> int:
    from .safety import leakcheck
    problems = leakcheck(a.folder)
    if problems:
        print("NOT SAFE TO PUBLISH:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"OK: no keys or tokens found in {a.folder}")
    return 0


def cmd_doctor(a) -> int:
    from .llm import LLM
    from .runner import NebiusRunner
    from .search import Tavily

    s = Settings.load()
    llm = LLM(s)
    print(f"Token Factory key : {'set' if llm.available else 'MISSING (NEBIUS_API_KEY)'}")
    if llm.available:
        for role in ("nano", "super", "ultra"):
            try:
                reply = llm.chat(role, "Reply with the single word OK.", "ping", max_tokens=50)
                print(f"  {role:<5} -> {llm.model_for(role)}  reply: {reply[:40]!r}")
            except Exception as e:
                print(f"  {role:<5} FAILED: {e}")
    t = Tavily(s)
    print(f"Tavily key        : {'set' if t.available else 'MISSING (TAVILY_API_KEY)'}")
    if t.available:
        r = t.search("Attention Is All You Need arxiv", 2)
        print(f"  search returned {len(r)} results")
    print(f"nebius CLI        : {'found' if NebiusRunner.cli_available() else 'NOT FOUND (needed for --runner nebius)'}")
    from .runner import DockerRunner
    import subprocess
    if DockerRunner.available():
        have = subprocess.run(["docker", "image", "inspect", s.job_image], capture_output=True).returncode == 0
        print(f"docker            : found; image {s.job_image} {'present locally' if have else 'NOT present locally (docker pull / build it)'}")
    else:
        print("docker            : NOT FOUND (needed for --runner docker)")
    if NebiusRunner.cli_available() and a.job:
        import os
        from .runner import JobSpec
        r = NebiusRunner(s.job_image, s.job_platform, s.job_preset, s.job_extra_args, s.job_timeout, s.job_cost_per_hour,
                         profile=os.environ.get("RECEIPTS_NEBIUS_PROFILE", ""),
                         parent_id=os.environ.get("RECEIPTS_NEBIUS_PARENT_ID", "")).run(
            JobSpec(script='import json,os\nprint("RECEIPTS_RESULT "+json.dumps({"value": 1.0, "metric":"smoke"}))\n', seed=0, run_id="doctor", install=False))
        print(f"  smoke job: ok={r.ok} id={r.job_id} {r.seconds:.0f}s {r.error}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="receipts", description="Does a paper's headline result actually reproduce?")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check one paper (arXiv link/id, or demo:leakage|reproduces|broken)")
    r.add_argument("input")
    r.add_argument("--seeds", type=int, default=10)
    r.add_argument("--runner", choices=["local", "docker", "nebius"], default=None)
    r.add_argument("--tolerance", type=float, default=None, help="absolute tolerance in the claim's units")
    r.add_argument("--claim", default="", help="which result to check, e.g. \"ETTh1 MSE, horizon 96\" (default: the headline)")
    r.add_argument("--out", default=None)
    r.set_defaults(fn=cmd_run)
    sv = sub.add_parser("serve", help="web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)
    v = sub.add_parser("verify", help="re-run a receipt and compare with its recorded numbers")
    v.add_argument("receipt")
    v.add_argument("--atol", type=float, default=0.05)
    v.set_defaults(fn=cmd_verify)
    b = sub.add_parser("bench", help="run a labelled list of papers and report agreement/cost/time")
    b.add_argument("papers", help="JSON list of {input, expected, note}")
    b.add_argument("--seeds", type=int, default=8)
    b.add_argument("--runner", choices=["local", "docker", "nebius"], default=None)
    b.add_argument("--out", default=None)
    b.set_defaults(fn=cmd_bench)
    si = sub.add_parser("site", help="build the static replay website (docs/) from out/ receipts")
    si.add_argument("--out", default="out")
    si.add_argument("--dir", default="docs")
    si.add_argument("--repo", default="", help="your public GitHub repo URL, shown in the footer")
    si.set_defaults(fn=cmd_site)
    bu = sub.add_parser("bundle", help="copy the publishable files into a clean folder (no .env) and safety-check it")
    bu.add_argument("--to", required=True, help="a NEW folder, e.g. /mnt/c/Users/<you>/Desktop/receipts-publish")
    bu.set_defaults(fn=cmd_bundle)
    lc = sub.add_parser("leakcheck", help="check a folder contains no API keys before you publish it")
    lc.add_argument("folder")
    lc.set_defaults(fn=cmd_leakcheck)
    d = sub.add_parser("doctor", help="check keys, models, CLI")
    d.add_argument("--job", action="store_true", help="also run a tiny smoke job on Nebius")
    d.set_defaults(fn=cmd_doctor)
    a = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 and choke on the verdict icons
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
