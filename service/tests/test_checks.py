import copy

from fastapi.testclient import TestClient

from receipts import stats
from service import worker
from service.db import Check, UsageRecord
from service.tests import fakes


def submit(client, url="https://arxiv.org/abs/2106.09685", **kw):
    return client.post("/checks", json={"arxiv_url": url, **kw})


def test_submit_requires_auth_and_valid_input(app, client, user):
    with TestClient(app) as anon:
        assert submit(anon).status_code == 401
    assert submit(client, "not a paper").status_code == 422
    r = submit(client, "arXiv:2106.09685v2")
    assert r.status_code == 202
    j = r.json()
    assert j["status"] == "queued" and j["input"] == "https://arxiv.org/abs/2106.09685"
    assert j["caps"]["gpu_minutes"] == 30 and j["seeds"] == 5


def test_demo_inputs_can_be_disabled(settings, user, client, app):
    app.state.settings.allow_demo_inputs = False
    assert submit(client, "demo:reproduces").status_code == 422


def test_full_flow_events_receipt_badge_signature(app, client, user, db, settings):
    claim = {"statement": "X gets 90% on toy", "metric": "accuracy", "value": 90.0, "unit": "%", "dataset": "toy"}
    cid = submit(client, claim=claim, seeds=3).json()["id"]
    assert client.get(f"/badge/{cid}.svg").text.count("queued") >= 1
    assert client.get(f"/receipts/{cid}").status_code == 404

    assert worker.work_once(db, settings) == cid
    call = fakes.CALLS[-1]
    assert call["runner"] == "docker" and call["seeds"] == 3
    assert call["claim_override"]["statement"] == claim["statement"]
    assert call["max_gpu_min"] == "30"

    j = client.get(f"/checks/{cid}").json()
    assert j["status"] == "done" and j["verdict"] == stats.REPRODUCED
    stages = [e["stage"] for e in j["events"]]
    assert stages[0] == "queue" and "ingest" in stages and stages[-1] == "receipt"
    last = j["events"][-1]["data"]
    assert "receipt" not in last and "json" not in last  # bulky data and server paths stripped
    assert j["badge_markdown"] == f"[![Receipts-verified](http://testserver/badge/{cid}.svg)](http://testserver/receipts/{cid})"
    seq = j["events"][1]["seq"]
    assert all(e["seq"] > seq for e in client.get(f"/checks/{cid}?after={seq}").json()["events"])

    with TestClient(app) as anon:  # receipts, badges and verification are public
        env = anon.get(f"/receipts/{cid}.json").json()
        assert "_paths" not in env["receipt"] and env["signature"]["alg"] == "Ed25519"
        html = anon.get(f"/receipts/{cid}").text
        assert "REPRODUCED" in html and "Signature" in html and env["signature"]["value"] in html
        assert anon.get(f"/receipts/{cid}", headers={"accept": "application/json"}).json()["check"]["id"] == cid
        svg = anon.get(f"/badge/{cid}.svg")
        assert svg.headers["content-type"].startswith("image/svg+xml") and "reproduced" in svg.text
        assert anon.get(f"/receipts/{cid}/verify").json()["valid"] is True
        assert anon.post("/verify", json={"receipt": env["receipt"], "signature": env["signature"]["value"]}).json()["valid"]
        assert anon.post("/verify", json={"receipt": env}).json()["valid"]  # whole envelope
        tampered = copy.deepcopy(env["receipt"])
        tampered["verdict"]["verdict"] = "discrepancy_found"
        v = anon.post("/verify", json={"receipt": tampered, "signature": env["signature"]["value"]}).json()
        assert v["hash_ok"] is False and v["valid"] is False
        forged = dict(tampered, sha256=v["sha256"])  # attacker re-hashes, cannot re-sign
        v = anon.post("/verify", json={"receipt": forged, "signature": env["signature"]["value"]}).json()
        assert v["hash_ok"] is True and v["signature_ok"] is False
        assert anon.get("/badge/nope.svg").status_code == 200 and "not found" in anon.get("/badge/nope.svg").text
        # someone else's check is private while it runs
        anon.post("/auth/signup", json={"email": "eve@example.org", "password": "evepassword"})
        assert anon.get(f"/checks/{cid}").status_code == 404

    with db.session() as s:
        u = s.query(UsageRecord).filter_by(check_id=cid).one()
        assert u.gpu_minutes == 1.5 and u.llm_tokens == 150
    assert client.get("/usage").json()["gpu_minutes_used"] == 1.5
    assert client.get("/checks").json()[0]["id"] == cid


def test_monthly_check_quota(client, user, db, settings):
    for _ in range(3):
        assert submit(client).status_code == 202
        worker.work_once(db, settings)  # finished checks release their GPU reservation
    r = submit(client)
    assert r.status_code == 402 and "3 checks per month" in r.json()["detail"]


def test_gpu_minute_budget(client, user, db, settings, monkeypatch):
    monkeypatch.setattr(fakes, "ok_factory", lambda emit, st: fakes.FakePipeline(emit, st, job_seconds=59 * 60 + 40))
    cid = submit(client).json()["id"]
    worker.work_once(db, settings)
    assert client.get(f"/checks/{cid}").json()["status"] == "done"
    r = submit(client)
    assert r.status_code == 402 and "GPU-minute" in r.json()["detail"]


def test_running_checks_reserve_their_cap(client, user):
    a = submit(client).json()
    b = submit(client).json()
    assert a["caps"]["gpu_minutes"] == 30 and b["caps"]["gpu_minutes"] == 30
    assert submit(client).status_code == 402  # 60 GPU-min all reserved, even though a check is left


def test_seeds_capped_by_plan(client, user):
    assert submit(client, seeds=50).json()["seeds"] == 5


def test_html_submit_and_watch_page(client, user, db, settings):
    r = client.post("/checks", data={"arxiv_url": "demo:reproduces", "seeds": "2"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/c/")
    cid = r.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/c/{cid}").text
    assert "Progress" in page and f"/badge/{cid}.svg" in page
    r = client.post("/checks", data={"arxiv_url": "garbage"})
    assert "expected an arXiv URL" in r.text
    with db.session() as s:
        assert s.get(Check, cid).seeds == 2
