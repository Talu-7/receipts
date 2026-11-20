"""Fake pipelines for tests: same interface as receipts.pipeline.Pipeline, no LLM, no Docker.

They live in an importable module so the worker's spawned child process can load them by dotted path.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

from receipts import receipt, stats

CALLS: list[dict] = []


def make_receipt(run_id: str, user_input: str, verdict: str = stats.REPRODUCED, job_seconds: float = 90.0,
                 claim: dict | None = None) -> dict:
    claim = claim or {"statement": "Model reaches 90% accuracy", "metric": "accuracy", "value": 90.0, "unit": "%",
                      "dataset": "toy", "where": "Table 1"}
    vals = [89.8, 90.1, 90.3]
    return {
        "version": 1, "receipt_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "synthetic_demo": True, "input": user_input, "paper": {"title": "A Fake Paper", "arxiv_id": None, "url": None},
        "claim": claim, "claims_all": [claim], "repo": {"url": None, "commit": None}, "evidence": [],
        "script": {"code": "print('hi')", "requirements": [], "sha256": "abc"}, "seeds_requested": 3,
        "runs": [{"seed": i, "ok": True, "value": v, "extras": {}, "seconds": job_seconds / 3, "job_id": f"j{i}",
                  "logs": "ok", "log_summary": "fine"} for i, v in enumerate(vals)],
        "verdict": {"verdict": verdict, "claimed": claim["value"], "stats": stats.summarize(vals), "tolerance": 1.0,
                    "reason": "fake", "claimed_in_range": True, "shortfall": 0.0},
        "diagnosis": None,
        "infra": {"backend": "fake", "image": "x", "platform": "x", "job_seconds": job_seconds, "wall_seconds": 1.0,
                  "est_cost_usd": 0.01, "models": None,
                  "llm_usage": {"super": {"calls": 1, "prompt_tokens": 100, "completion_tokens": 50}}},
    }


class FakePipeline:
    def __init__(self, emit, settings=None, verdict=stats.REPRODUCED, sleep: float = 0.0, crash: bool = False,
                 job_seconds: float = 90.0):
        self._emit, self.run_id = emit, uuid.uuid4().hex[:8]
        self.verdict, self.sleep, self.crash, self.job_seconds = verdict, sleep, crash, job_seconds

    def emit(self, stage, status, msg, **data):
        self._emit({"t": time.time(), "run_id": self.run_id, "stage": stage, "status": status, "msg": msg, "data": data})

    def run(self, user_input, seeds=10, runner=None, claim_override=None, **kw):
        CALLS.append({"input": user_input, "seeds": seeds, "runner": runner, "claim_override": claim_override,
                      "max_gpu_min": os.environ.get("RECEIPTS_MAX_GPU_MIN")})
        self.emit("ingest", "start", f"Reading {user_input}")
        if self.sleep:
            time.sleep(self.sleep)
        if self.crash:
            raise RuntimeError("boom")
        self.emit("run", "done", "3/3 runs produced a number")
        rec = make_receipt(self.run_id, user_input, self.verdict, self.job_seconds, claim=claim_override and
                           {"where": "-", "unit": "", **claim_override})
        rec["sha256"] = receipt.canonical_hash(rec)
        rec["_paths"] = {"json": "/srv/out/x.json", "html": "/srv/out/x.html"}
        self.emit("receipt", "done", "Receipt written", json="/srv/out/x.json", verdict=self.verdict, receipt={"big": 1})
        return rec


# dotted-path factories (SERVICE_PIPELINE_FACTORY=service.tests.fakes:<name>)
def ok_factory(emit, settings):
    return FakePipeline(emit, settings)


def slow_factory(emit, settings):
    return FakePipeline(emit, settings, sleep=60)


def crash_factory(emit, settings):
    return FakePipeline(emit, settings, crash=True)


def hard_exit_factory(emit, settings):
    class P(FakePipeline):
        def run(self, *a, **k):
            os._exit(3)
    return P(emit, settings)
