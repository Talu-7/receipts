"""Queue worker: claims queued checks, runs the Receipts pipeline under a cap, stores events and a signed receipt.

    python -m service.worker            # loop forever
    python -m service.worker --once     # process at most one check, then exit

Each check runs in its own child process (SERVICE_EXECUTOR=process) so the worker can kill it when it
exceeds the plan's wall-clock cap. The child streams the pipeline's `emit` events straight into the
database. Any way a check can end badly (cap hit, crash, worker death) still ends in an honest
INCONCLUSIVE receipt that says why, signed like any other.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib
import json
import logging
import multiprocessing as mp
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update

from receipts import receipt as receipt_mod
from receipts import stats

from .config import ServiceSettings
from .db import Check, CheckEvent, Database, utcnow
from .quotas import meter
from .signing import load_signer

log = logging.getLogger("receipts.service.worker")
MAX_EVENT_DATA = 20_000
DROP_DATA_KEYS = {"receipt", "json", "html"}  # bulky, or server-local file paths


# ---------------------------------------------------------------- pipeline factory
def default_pipeline_factory(emit: Callable[[dict], None], settings: ServiceSettings):
    """Builds the real core Pipeline. Kept import-light so tests never touch LLMs or Docker."""
    from receipts.config import Settings
    from receipts.pipeline import Pipeline

    s = Settings.load()
    s.out_dir = os.path.join(settings.data_dir, "receipts")
    s.default_runner = settings.runner
    return Pipeline(s, emit=emit)


def resolve_factory(path: str):
    mod, _, attr = path.partition(":")
    return getattr(importlib.import_module(mod), attr)


# ---------------------------------------------------------------- queue
def claim_next(db: Database, worker_id: str) -> str | None:
    """Atomically move the oldest queued check to running. Portable: a conditional UPDATE, no row locks needed."""
    with db.session() as s:
        for cid in s.scalars(select(Check.id).where(Check.status == "queued").order_by(Check.created_at).limit(5)):
            now = utcnow()
            res = s.execute(update(Check).where(Check.id == cid, Check.status == "queued").values(
                status="running", worker_id=worker_id, started_at=now, heartbeat_at=now, attempts=Check.attempts + 1))
            if res.rowcount == 1:
                return cid
    return None


def recover_stale(db: Database, settings: ServiceSettings) -> list[str]:
    """Checks whose worker died mid-run: requeue once, then give up honestly."""
    cutoff = utcnow() - timedelta(seconds=settings.heartbeat_stale_s)
    out = []
    with db.session() as s:
        for c in s.scalars(select(Check).where(Check.status == "running")).all():
            hb = c.heartbeat_at
            if hb is not None and hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            if hb is not None and hb >= cutoff:
                continue
            out.append(c.id)
            if c.attempts < 2:
                c.status, c.worker_id = "queued", None
                _add_event(s, c.id, "queue", "warn", "worker stopped responding; check re-queued")
            else:
                _finalize_failure(s, c, "worker stopped responding twice; the check was abandoned", settings)
    return out


# ---------------------------------------------------------------- events
def _clean_data(data: dict | None) -> dict | None:
    if not data:
        return None
    d = {k: v for k, v in data.items() if k not in DROP_DATA_KEYS}
    try:
        txt = json.dumps(d, default=str)
    except Exception:
        return {"note": "unserialisable event data dropped"}
    if len(txt) > MAX_EVENT_DATA:
        return {"note": f"event data truncated ({len(txt)} bytes)"}
    return json.loads(txt)


def _add_event(s, check_id: str, stage: str, status: str, msg: str, data: dict | None = None, t: float | None = None):
    s.add(CheckEvent(check_id=check_id, t=t or time.time(), stage=str(stage)[:32], status=str(status)[:16],
                     msg=str(msg)[:4000], data=_clean_data(data)))


def make_emitter(db: Database, check_id: str) -> Callable[[dict], None]:
    lock = threading.Lock()  # the pipeline emits from its thread pool
    seen_run_id = {"v": None}

    def emit(e: dict) -> None:
        try:
            with lock, db.session() as s:
                _add_event(s, check_id, e.get("stage", "?"), e.get("status", "?"), e.get("msg", ""), e.get("data"), e.get("t"))
                rid = e.get("run_id")
                vals = {"heartbeat_at": utcnow()}
                if rid and seen_run_id["v"] != rid:
                    seen_run_id["v"] = rid
                    vals["run_id"] = rid
                s.execute(update(Check).where(Check.id == check_id).values(**vals))
        except Exception:  # progress reporting must never kill a run
            log.exception("could not store event for %s", check_id)
    return emit


# ---------------------------------------------------------------- receipts
def failure_receipt(check: Check, reason: str, run_id: str | None = None) -> dict:
    """Same shape as the core pipeline's failure receipt, so the core HTML renderer works on it."""
    co = check.claim_override or {}
    claim = {"statement": co.get("statement") or "(could not be established)", "value": float(co.get("value") or 0.0),
             "metric": co.get("metric") or "-", "dataset": co.get("dataset") or "-", "where": co.get("where") or "-"}
    v = {"verdict": stats.INCONCLUSIVE, "claimed": claim["value"], "stats": stats.summarize([]), "tolerance": None,
         "reason": reason, "claimed_in_range": None, "shortfall": None}
    return {"version": 1, "receipt_id": run_id or check.id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "synthetic_demo": False, "input": check.arxiv_url, "paper": {"title": check.arxiv_url, "url": None},
            "claim": claim, "claims_all": [], "repo": {}, "evidence": [],
            "script": {"code": "", "requirements": [], "sha256": ""}, "seeds_requested": 0, "runs": [], "verdict": v,
            "diagnosis": {"category": "cannot_determine", "explanation": reason, "evidence": [], "confidence": 1.0,
                          "suggested_check": "This is a limit of the hosted service run, not a finding about the paper.",
                          "source": "service"},
            "infra": {"backend": "service", "image": None, "platform": None, "job_seconds": 0.0, "wall_seconds": 0.0,
                      "est_cost_usd": 0.0, "models": None, "llm_usage": {}},
            "service": {"check_id": check.id, "failure": reason}}


def _normalise(rec: dict) -> dict:
    """JSON round-trip (what we store and serve), and a hash that matches the stored form."""
    body = json.loads(json.dumps({k: v for k, v in rec.items() if k != "_paths"}, default=str))
    if body.get("sha256") != receipt_mod.canonical_hash(body):
        body["sha256"] = receipt_mod.canonical_hash(body)
    return body


def _store_receipt(s, check: Check, rec: dict, status: str, failure: str | None, settings: ServiceSettings):
    body = _normalise(rec)
    signer = load_signer(settings)
    check.receipt, check.receipt_sha256 = body, body["sha256"]
    check.signature, check.signing_key_id = signer.sign_hash(body["sha256"]), signer.key_id
    check.verdict = (body.get("verdict") or {}).get("verdict") or stats.INCONCLUSIVE
    check.status, check.failure_reason, check.finished_at = status, failure, utcnow()
    started = check.started_at
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    wall = (check.finished_at - started).total_seconds() if started else 0.0
    meter(s, check, body, wall)


def _finalize_failure(s, check: Check, reason: str, settings: ServiceSettings):
    _add_event(s, check.id, "error", "error", reason)
    _store_receipt(s, check, failure_receipt(check, reason, check.run_id), "failed", reason, settings)


def finalize(db: Database, settings: ServiceSettings, check_id: str, rec: dict | None, failure: str | None) -> bool:
    """Write the outcome once. Returns False if someone else already finished this check."""
    with db.session() as s:
        c = s.get(Check, check_id)
        if c is None or c.status != "running":
            return False
        if rec is not None and failure is None:
            _store_receipt(s, c, rec, "done", None, settings)
        else:
            _finalize_failure(s, c, failure or "the check failed without a reason", settings)
        return True


# ---------------------------------------------------------------- one check
def _run_kwargs(check: Check) -> dict:
    kw = {"seeds": check.seeds}
    if check.claim_override:
        kw["claim_override"] = dict(check.claim_override)
    return kw


def run_check_here(db: Database, settings: ServiceSettings, check_id: str, factory: Callable | None = None) -> None:
    """Runs one check in the current process and finalises it. Used by the child process and by tests."""
    with db.session() as s:
        c = s.get(Check, check_id)
        user_input, kw, cap = c.arxiv_url, _run_kwargs(c), c.gpu_minutes_cap
    os.environ["RECEIPTS_MAX_GPU_MIN"] = f"{cap:g}"  # the core's feasibility gate reads this
    factory = factory or resolve_factory(settings.pipeline_factory)
    emit = make_emitter(db, check_id)
    try:
        p = factory(emit, settings)
        rid = getattr(p, "run_id", None)
        if rid:
            with db.session() as s:
                s.execute(update(Check).where(Check.id == check_id).values(run_id=rid))
        rec = p.run(user_input, runner=settings.runner, **kw)
        if not isinstance(rec, dict) or "verdict" not in rec:
            raise RuntimeError("the pipeline returned no receipt")
        finalize(db, settings, check_id, rec, None)
    except Exception as e:  # the core already turns most failures into receipts; this is the last guard
        log.exception("check %s crashed", check_id)
        finalize(db, settings, check_id, None, f"the reproduction crashed: {type(e).__name__}: {e}")


def _child_main(db_url: str, settings_dict: dict, check_id: str) -> None:
    logging.basicConfig(level=logging.INFO)
    settings = ServiceSettings(**settings_dict)
    run_check_here(Database(db_url), settings, check_id)


def _kill_sandboxes(run_id: str | None) -> None:
    """The core's DockerRunner names containers receipts-<run_id>-s<seed>-...; remove any left behind."""
    if not run_id:
        return
    try:
        p = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=receipts-{run_id}-"], capture_output=True,
                           text=True, timeout=30)
        ids = p.stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=60)
    except Exception:
        pass


def process_check(db: Database, settings: ServiceSettings, check_id: str, factory: Callable | None = None) -> None:
    with db.session() as s:
        c = s.get(Check, check_id)
        cap_s = c.wall_seconds_cap
        _add_event(s, check_id, "queue", "start", f"Picked up by {c.worker_id}; caps {c.gpu_minutes_cap:g} GPU-min, "
                                                  f"{cap_s // 60} min wall clock, {c.seeds} seeds")
    if settings.executor == "inline":
        _process_inline(db, settings, check_id, cap_s, factory)
        return
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_child_main, args=(db.url, asdict(settings), check_id), daemon=False)
    proc.start()
    deadline = time.time() + cap_s
    while proc.is_alive() and time.time() < deadline:
        proc.join(timeout=min(10, max(0.1, deadline - time.time())))
        with db.session() as s:
            s.execute(update(Check).where(Check.id == check_id, Check.status == "running").values(heartbeat_at=utcnow()))
    if proc.is_alive():
        proc.terminate()
        proc.join(10)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        with db.session() as s:
            run_id = s.get(Check, check_id).run_id
        _kill_sandboxes(run_id)
        finalize(db, settings, check_id, None, f"time cap reached: the check was stopped after {cap_s // 60} minutes "
                                               f"(plan limit). This is a scope limit, not evidence against the paper.")
        return
    if proc.exitcode != 0:
        finalize(db, settings, check_id, None, f"the reproduction process exited with code {proc.exitcode}")
    else:
        finalize(db, settings, check_id, None, "the reproduction process ended without writing a receipt")  # no-op if done


def _process_inline(db, settings, check_id, cap_s, factory):
    t = threading.Thread(target=run_check_here, args=(db, settings, check_id, factory), daemon=True)
    t.start()
    t.join(cap_s)
    if t.is_alive():  # cannot kill a thread: record the cap; a late result is ignored by finalize()
        finalize(db, settings, check_id, None, f"time cap reached: the check was stopped after {cap_s} s (plan limit). "
                                               f"This is a scope limit, not evidence against the paper.")


# ---------------------------------------------------------------- loop
def work_once(db: Database, settings: ServiceSettings, worker_id: str | None = None, factory: Callable | None = None) -> str | None:
    wid = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    cid = claim_next(db, wid)
    if cid:
        log.info("processing check %s", cid)
        process_check(db, settings, cid, factory)
    return cid


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Receipts service worker")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = ServiceSettings.from_env()
    db = Database(settings.database_url)
    db.create_all()
    load_signer(settings)  # fail fast if the signing key is unusable
    recovered = recover_stale(db, settings)
    if recovered:
        log.warning("recovered stale checks: %s", recovered)
    log.info("worker started (executor=%s runner=%s)", settings.executor, settings.runner)
    while True:
        cid = work_once(db, settings)
        if a.once:
            return
        if not cid:
            time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
