from dataclasses import replace
from datetime import timedelta

from receipts import stats
from service import worker
from service.db import Check, utcnow


def _submit(client):
    r = client.post("/checks", json={"arxiv_url": "https://arxiv.org/abs/2106.09685"})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _get(client, cid):
    return client.get(f"/checks/{cid}").json()


def test_claim_is_atomic(client, user, db):
    cid = _submit(client)
    assert worker.claim_next(db, "w1") == cid
    assert worker.claim_next(db, "w2") is None
    with db.session() as s:
        c = s.get(Check, cid)
        assert c.status == "running" and c.worker_id == "w1" and c.attempts == 1


def test_crash_becomes_signed_inconclusive_receipt(client, user, db, settings):
    cid = _submit(client)
    worker.work_once(db, replace(settings, pipeline_factory="service.tests.fakes:crash_factory"))
    j = _get(client, cid)
    assert j["status"] == "failed" and j["verdict"] == stats.INCONCLUSIVE
    assert "RuntimeError: boom" in j["failure_reason"]
    assert j["events"][-1]["status"] == "error"
    assert client.get(f"/receipts/{cid}/verify").json()["valid"]
    assert "INCONCLUSIVE" in client.get(f"/receipts/{cid}").text
    assert "inconclusive" in client.get(f"/badge/{cid}.svg").text


def test_inline_time_cap(client, user, db, settings):
    cid = _submit(client)
    with db.session() as s:
        s.get(Check, cid).wall_seconds_cap = 1
    worker.work_once(db, replace(settings, pipeline_factory="service.tests.fakes:slow_factory"))
    j = _get(client, cid)
    assert j["status"] == "failed" and j["verdict"] == stats.INCONCLUSIVE
    assert j["failure_reason"].startswith("time cap reached") and "not evidence against the paper" in j["failure_reason"]


def test_process_executor_success(client, user, db, settings):
    cid = _submit(client)
    worker.work_once(db, replace(settings, executor="process"))
    j = _get(client, cid)
    assert j["status"] == "done" and j["verdict"] == stats.REPRODUCED, j
    assert any(e["stage"] == "ingest" for e in j["events"])  # streamed from the child process
    assert client.get(f"/receipts/{cid}/verify").json()["valid"]


def test_process_executor_kills_on_time_cap(client, user, db, settings):
    cid = _submit(client)
    with db.session() as s:
        s.get(Check, cid).wall_seconds_cap = 3
    worker.work_once(db, replace(settings, executor="process", pipeline_factory="service.tests.fakes:slow_factory"))
    j = _get(client, cid)
    assert j["status"] == "failed" and j["failure_reason"].startswith("time cap reached")
    with db.session() as s:  # a killed check is metered at wall time
        from service.db import UsageRecord
        assert s.query(UsageRecord).filter_by(check_id=cid).one().gpu_minutes > 0


def test_process_executor_hard_exit(client, user, db, settings):
    cid = _submit(client)
    worker.work_once(db, replace(settings, executor="process", pipeline_factory="service.tests.fakes:hard_exit_factory"))
    j = _get(client, cid)
    assert j["status"] == "failed" and "exited with code 3" in j["failure_reason"]


def test_stale_running_check_is_requeued_then_abandoned(client, user, db, settings):
    cid = _submit(client)
    for attempt in (1, 2):
        assert worker.claim_next(db, "dead") == cid
        with db.session() as s:
            s.get(Check, cid).heartbeat_at = utcnow() - timedelta(hours=1)
        assert worker.recover_stale(db, settings) == [cid]
        status = _get(client, cid)["status"]
        assert status == ("queued" if attempt == 1 else "failed")
    assert "abandoned" in _get(client, cid)["failure_reason"]


def test_late_result_is_ignored(client, user, db, settings):
    cid = _submit(client)
    worker.claim_next(db, "w")
    assert worker.finalize(db, settings, cid, None, "time cap reached: test")
    assert not worker.finalize(db, settings, cid, {"verdict": {"verdict": "reproduced"}}, None)
    assert _get(client, cid)["verdict"] == stats.INCONCLUSIVE


def test_event_data_is_sanitised():
    assert worker._clean_data({"receipt": {"x": 1}, "html": "/p", "ok": 1}) == {"ok": 1}
    assert "truncated" in worker._clean_data({"big": "x" * 30_000})["note"]
    assert worker._clean_data(None) is None


def test_default_factory_builds_core_pipeline(settings, monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    p = worker.default_pipeline_factory(lambda e: None, settings)
    assert p.s.default_runner == "docker" and p.s.out_dir.endswith("receipts")
    assert hasattr(p, "run") and p.run_id
