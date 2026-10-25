"""The Receipts agent: arXiv link -> claims -> code/data -> parallel sandboxed runs -> verdict -> receipt."""
from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from . import arxiv_src, claims as claims_mod, codegen, demo_cases, diagnose as diag_mod, receipt, stats, summarize
from .config import Settings
from .discover import discover, filter_evidence, readme_notice, repo_context
from .llm import LLM
from .runner import DockerRunner, JobResult, JobSpec, LocalRunner, NebiusRunner
from .search import Tavily

Emit = Callable[[dict], None]


def make_runner(s: Settings, name: str):
    if name == "nebius":
        if not NebiusRunner.cli_available():
            raise RuntimeError("the `nebius` CLI was not found; install/configure it or use --runner local")
        return NebiusRunner(s.job_image, s.job_platform, s.job_preset, s.job_extra_args, s.job_timeout, s.job_cost_per_hour,
                            profile=os.environ.get("RECEIPTS_NEBIUS_PROFILE", ""), parent_id=os.environ.get("RECEIPTS_NEBIUS_PARENT_ID", ""))
    if name == "docker":
        if not DockerRunner.available():
            raise RuntimeError("`docker` was not found; start Docker Desktop (enable WSL integration) or use --runner local")
        return DockerRunner(s.job_image, os.environ.get("RECEIPTS_DOCKER_MEMORY", "4g"), os.environ.get("RECEIPTS_DOCKER_CPUS", "2"),
                            gpus=os.environ.get("RECEIPTS_DOCKER_GPUS", "0") == "1")
    return LocalRunner()


def guard_reduced_scale(verdict: dict, extras_list: list[dict]) -> None:
    """A run that was shrunk (fewer steps, subset, smaller model) is not comparable to the claim.

    If the script declared deviations, a worse-than-claimed number must never be reported as a
    discrepancy: downgrade to inconclusive and keep the numbers for the record.
    """
    devs = []
    for ex in extras_list:
        d = (ex or {}).get("deviations")
        if d:
            devs.append(d if isinstance(d, str) else json.dumps(d))
    if not devs:
        return
    verdict["deviations"] = devs[0][:400]
    if verdict["verdict"] in (stats.DISCREPANCY, stats.PARTIAL):
        verdict["verdict_at_reduced_scale"] = verdict["verdict"]
        verdict["verdict"] = stats.INCONCLUSIVE
        verdict["reduced_scale"] = True
        verdict["reason"] = ("The reproduction was run at reduced scale (" + devs[0][:220] +
                             "), so its numbers are not comparable to the claim. This is not evidence against the paper.")
    elif verdict["verdict"] == stats.REPRODUCED:
        verdict["reason"] = (verdict.get("reason") or "") + " (run at reduced scale: " + devs[0][:160] + ")"


class Pipeline:
    def __init__(self, settings: Settings | None = None, emit: Emit | None = None):
        self._partial: dict = {}  # what we established before a failure, so the receipt can still show it
        self.s = settings or Settings.load()
        self.llm = LLM(self.s)
        self.search = Tavily(self.s)
        self._emit = emit or (lambda e: None)
        self.run_id = uuid.uuid4().hex[:8]

    def emit(self, stage: str, status: str, msg: str, **data):
        self._emit({"t": time.time(), "run_id": self.run_id, "stage": stage, "status": status, "msg": msg, "data": data})

    # ------------------------------------------------------------------ main entry
    def run(self, user_input: str, seeds: int = 10, runner: str | None = None, tolerance: float | None = None,
            claim_hint: str = "") -> dict:
        runner_name = runner or self.s.default_runner
        t_start = time.time()
        self.emit("ingest", "start", f"Reading {user_input}")
        case = demo_cases.get_case(user_input) if user_input.strip().lower().startswith("demo:") else None
        try:
            if case:
                paper = {"title": case["title"], "arxiv_id": None, "url": None, "abstract": case["blurb"]}
                claim, all_claims = case["claim"], [case["claim"]]
                repo, evidence = {"url": None, "commit": None}, []
                script, reqs = case["script"], case["requirements"]
                bundled = True
                self.emit("ingest", "done", f"Bundled synthetic case: {case['title']}")
                self.emit("claims", "done", f"Claim: {claim['metric']} = {claim['value']}{claim['unit']}", claim=claim)
                self.emit("discover", "done", "Synthetic case: no repo/errata search needed")
                self.emit("codegen", "done", "Using the bundled reproduction script")
            else:
                paper, claim, all_claims, repo, evidence, script, reqs = self._prepare_real(user_input, claim_hint)
                bundled = False
        except Exception as e:  # always return a receipt, never a stack trace
            self.emit("error", "error", f"{type(e).__name__}: {e}")
            return self._finish_failed(user_input, str(e), t_start, runner_name)

        try:
            run = make_runner(self.s, runner_name)
        except Exception as e:  # e.g. docker/nebius CLI missing: still return an honest receipt
            self.emit("error", "error", str(e))
            return self._finish_failed(user_input, str(e), t_start, runner_name)
        results = self._execute(run, script, reqs, claim, paper, repo, bundled, seeds, evidence)
        results, script, reqs = results

        self.emit("analyze", "start", "Computing verdict")
        values = [r.value for r in results if r.ok and r.value is not None]
        verdict = stats.decide(claim["value"], values, len(results), tol=tolerance,
                               higher_is_better=claim.get("higher_is_better", True))
        for r in results:  # repo/commit pinned by the sandbox
            if r.meta.get("repo_commit") and not repo.get("commit"):
                repo["commit"] = r.meta["repo_commit"]
        guard_reduced_scale(verdict, [r.extras for r in results if r.ok])
        self.emit("analyze", "done", f"Verdict: {verdict['verdict']}", verdict=verdict)

        per_seed = [r.to_dict() for r in results]
        self.emit("summarize", "start", "Nemotron Nano: summarising logs" if self.llm.available else "Summarising logs")
        order = [r for r in results if not r.ok][:3] + [r for r in results if r.ok][:1]
        summ = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {r.seed: ex.submit(summarize.summarize_logs, self.llm, r.logs, r.ok) for r in order}
            for k, f in futs.items():
                summ[k] = f.result()
        for d, r in zip(per_seed, results):
            d["log_summary"] = summ.get(r.seed, "")
            d["logs"] = r.logs[-6000:]
        self.emit("summarize", "done", "Logs summarised")

        diagnosis = None
        if verdict["verdict"] != stats.REPRODUCED:
            self.emit("diagnose", "start", "Nemotron Ultra: reasoning about the gap" if self.llm.available else "Diagnosing (offline heuristic)")
            diagnosis = diag_mod.diagnose(self.llm, claim, verdict, per_seed, list(summ.values()), script, evidence)
            self.emit("diagnose", "done", f"Cause: {diagnosis['category']}", diagnosis=diagnosis)

        total = sum(r.seconds for r in results)
        est = sum((r.meta.get("est_cost_usd") or 0) for r in results)
        first_meta = next((r.meta for r in results if r.meta), {})
        rec = {
            "version": 1, "receipt_id": self.run_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "synthetic_demo": bool(case), "input": user_input, "paper": paper, "claim": claim, "claims_all": all_claims,
            "repo": repo, "evidence": evidence,
            "script": {"code": script, "requirements": reqs, "sha256": first_meta.get("script_sha256", "")},
            "seeds_requested": len(results), "runs": [], "verdict": verdict, "diagnosis": diagnosis,
            "infra": {"backend": run.name, "image": first_meta.get("image", "local-python"), "platform": first_meta.get("platform", "local"),
                      "job_seconds": total, "wall_seconds": time.time() - t_start, "est_cost_usd": est,
                      "models": {"nano": self.llm.model_for("nano"), "super": self.llm.model_for("super"), "ultra": self.llm.model_for("ultra")} if self.llm.available else None,
                      "llm_usage": self.llm.usage},
        }
        rec["runs"] = per_seed
        jp, hp = receipt.write(rec, self.s.out_dir)
        rec["_paths"] = {"json": jp, "html": hp}
        self.emit("receipt", "done", "Receipt written", json=jp, html=hp, verdict=verdict["verdict"], receipt=_public(rec))
        return rec

    # ------------------------------------------------------------------ real-paper preparation
    def _prepare_real(self, user_input: str, claim_hint: str = ""):
        if not self.llm.available:
            raise RuntimeError("real papers need NEBIUS_API_KEY (Nemotron). Try a bundled case: demo:leakage, demo:reproduces, demo:broken")
        aid = arxiv_src.parse_arxiv_id(user_input)
        if not aid:
            raise RuntimeError("could not find an arXiv id in the input")
        meta = arxiv_src.fetch_metadata(aid)
        text = arxiv_src.fetch_fulltext(aid)
        self.emit("ingest", "done", f"{meta['title']} ({len(text):,} chars of full text)", title=meta["title"])
        self._partial = {"paper": {"title": meta["title"], "arxiv_id": aid, "url": meta["url"], "abstract": meta["abstract"]}}

        self.emit("claims", "start", "Nemotron Super: extracting claims")
        cl = claims_mod.extract_claims(self.llm, meta, text, hint=claim_hint)
        claim = cl["headline"]
        self.emit("claims", "done", f"Headline: {claim['statement']}", claim=claim, claims=cl["claims"])
        self._partial.update(claim=claim, claims=cl["claims"])

        # Feasibility gate: do not burn jobs on a claim that cannot be re-run within budget.
        max_min = float(os.environ.get("RECEIPTS_MAX_GPU_MIN", "60"))
        est_min = claim.get("est_gpu_minutes")
        if claim.get("tractable") is False or (isinstance(est_min, (int, float)) and est_min > max_min):
            raise RuntimeError(
                f"Not attempted: re-running this headline claim is estimated at "
                f"{est_min if est_min is not None else 'more than the budget of'} GPU-minutes "
                f"(budget {max_min:.0f}). This is a scope limit, not a finding about the paper. "
                f"Raise RECEIPTS_MAX_GPU_MIN to override.")

        self.emit("discover", "start", "Tavily: repo, datasets, errata, prior replications")
        disc = discover(self.search, meta, text, claim.get("dataset") or "")
        # Tavily returns generic pages; keep only results that really report an erratum or a replication.
        disc["evidence"] = filter_evidence(self.llm, meta["title"], disc["evidence"], aid)
        self._partial["evidence"] = disc["evidence"]
        if not disc["repo_url"]:
            raise RuntimeError("no public code repository found for this paper (searched the paper text and the web)")
        ctx = repo_context(disc["repo_url"], claim.get("dataset") or "")
        notice = readme_notice(ctx.get("readme", ""), disc["repo_url"])
        if notice:
            disc["evidence"].insert(0, notice)
        ctx["data_links"] = [e["url"] for e in disc["evidence"] if e["kind"] == "data"][:4]
        repo = {"url": disc["repo_url"], "commit": ctx.get("commit")}
        self.emit("discover", "done", f"Repo {disc['repo_url']}; {len(disc['evidence'])} evidence links", repo=repo, evidence=disc["evidence"])

        self.emit("codegen", "start", "Nemotron Super: writing the reproduction script")
        gen = codegen.generate_script(self.llm, meta, claim, disc["repo_url"], ctx)
        self.emit("codegen", "done", "Reproduction script ready", requirements=gen["requirements"])
        self._ctx, self._meta = ctx, meta
        paper = {"title": meta["title"], "arxiv_id": aid, "url": meta["url"], "abstract": meta["abstract"]}
        return paper, claim, cl["claims"], repo, disc["evidence"], gen["script"], gen["requirements"]

    # ------------------------------------------------------------------ sandboxed execution
    def _spec(self, script, reqs, repo, seed, bundled) -> JobSpec:
        return JobSpec(script=script, seed=seed, run_id=self.run_id, repo_url=repo.get("url"), repo_commit=repo.get("commit"),
                       requirements=reqs, install=not bundled, install_repo_requirements=not bundled and bool(repo.get("url")),
                       timeout_s=1500,
                       # parallel CPU demo jobs must not each grab every core (thread oversubscription made them 10x slower)
                       env={"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"} if bundled else {})

    def _one(self, run, spec: JobSpec, claim: dict | None = None) -> JobResult:
        self.emit("run", "start", f"seed {spec.seed}: started", seed=spec.seed)
        res = run.run(spec)
        bad = stats.implausible(res.value, claim) if (res.ok and claim) else None
        if bad:  # a placeholder is a failed run, so the repair loop gets to fix the script
            res.ok, res.error, res.meta["rejected_value"] = False, f"rejected value: {bad}", res.value
            res.value = None
        self.emit("run", "done" if res.ok else "warn",
                  f"seed {spec.seed}: {res.value:.3f}" if res.ok else f"seed {spec.seed}: failed ({res.error})",
                  seed=spec.seed, ok=res.ok, value=res.value, seconds=res.seconds, extras=res.extras)
        return res

    def _execute(self, run, script, reqs, claim, paper, repo, bundled, n_seeds, evidence):
        seeds = list(range(n_seeds))
        par = max(1, min(self.s.max_parallel, n_seeds))
        self.emit("run", "start", f"Running {n_seeds} seeds on {run.name} (parallelism {par})", seeds=n_seeds, backend=run.name)
        results: list[JobResult] = []
        if not bundled:  # probe one seed first and let Super repair the script if it breaks
            first = self._one(run, self._spec(script, reqs, repo, seeds[0], bundled), claim)
            attempts = 0
            while not first.ok and attempts < 3:
                attempts += 1
                fail = summarize.summarize_logs(self.llm, first.logs, False)
                self.emit("codegen", "start", f"Probe failed ({fail[:140]}). Super is repairing the script (attempt {attempts})")
                try:
                    gen = codegen.generate_script(self.llm, self._meta, claim, repo["url"], self._ctx, previous=script, failure=fail + "\n" + first.logs[-1500:])
                except Exception as e:
                    self.emit("codegen", "warn", f"repair failed: {e}")
                    break
                script, reqs = gen["script"], gen["requirements"]
                first = self._one(run, self._spec(script, reqs, repo, seeds[0], bundled), claim)
            if attempts:
                if first.ok:
                    self.emit("codegen", "done", f"Script repaired after {attempts} attempt(s)")
                else:
                    self.emit("codegen", "warn", f"Could not get the script to run after {attempts} repair attempt(s)")
            results.append(first)
            seeds = seeds[1:] if first.ok else []  # no point burning N jobs on a script that cannot run
        with ThreadPoolExecutor(max_workers=par) as ex:
            futs = [ex.submit(self._one, run, self._spec(script, reqs, repo, s, bundled), claim) for s in seeds]
            for f in as_completed(futs):
                results.append(f.result())
        results.sort(key=lambda r: r.seed)
        self.emit("run", "done", f"{sum(r.ok for r in results)}/{len(results)} runs produced a number")
        return results, script, reqs

    # ------------------------------------------------------------------ failure path
    def _finish_failed(self, user_input, reason, t_start, runner_name) -> dict:
        pt = self._partial
        claim = pt.get("claim") or {"statement": "(could not be established)", "value": 0.0, "metric": "-", "dataset": "-", "where": "-"}
        v = {"verdict": stats.INCONCLUSIVE, "claimed": claim["value"], "stats": stats.summarize([]), "tolerance": None,
             "reason": reason, "claimed_in_range": None, "shortfall": None}
        rec = {"version": 1, "receipt_id": self.run_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "synthetic_demo": False, "input": user_input, "paper": pt.get("paper") or {"title": user_input, "url": None},
               "claim": claim, "claims_all": pt.get("claims", []), "repo": {}, "evidence": pt.get("evidence", []),
               "script": {"code": "", "requirements": [], "sha256": ""},
               "seeds_requested": 0, "runs": [], "verdict": v,
               "diagnosis": {"category": "cannot_determine", "explanation": reason, "evidence": [], "confidence": 1.0,
                             "suggested_check": "Fix the blocker above and re-run.", "source": "pipeline"},
               "infra": {"backend": runner_name, "image": None, "platform": None, "job_seconds": 0.0,
                         "wall_seconds": time.time() - t_start, "est_cost_usd": 0.0, "models": None, "llm_usage": self.llm.usage}}
        jp, hp = receipt.write(rec, self.s.out_dir)
        rec["_paths"] = {"json": jp, "html": hp}
        self.emit("receipt", "done", "Receipt written (inconclusive)", json=jp, html=hp, verdict="inconclusive", receipt=_public(rec))
        return rec


def _public(rec: dict) -> dict:
    """Receipt without bulky logs, for streaming to the UI."""
    r = {k: v for k, v in rec.items() if k != "runs"}
    r["runs"] = [{k: v for k, v in x.items() if k != "logs"} for x in rec["runs"]]
    return r
