import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from service import badge, billing, signing
from service.tests.conftest import WEBHOOK_SECRET


def _signed(payload: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    """Builds a Stripe-Signature header the way Stripe does (t=..,v1=HMAC-SHA256)."""
    body = json.dumps(payload).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


def _event(ev_id, typ, obj, livemode=False):
    return {"id": ev_id, "object": "event", "type": typ, "livemode": livemode, "data": {"object": obj}}


def _post(client, payload, secret=WEBHOOK_SECRET):
    body, sig = _signed(payload, secret)
    return client.post("/billing/webhook", content=body, headers={"Stripe-Signature": sig, "Content-Type": "application/json"})


def test_live_keys_are_refused():
    for bad in ("", "sk_live_abc", "pk_test_abc", "rk_live_x"):
        with pytest.raises(billing.BillingError):
            billing.require_test_key(bad)
    assert billing.require_test_key("sk_test_abc") == "sk_test_abc"


def test_checkout_uses_test_key_and_price(client, user, monkeypatch, app):
    seen = {}

    def fake_create(**kw):
        seen.update(kw)
        return {"url": "https://checkout.stripe.com/c/pay/cs_test_123"}

    monkeypatch.setattr(billing.stripe.checkout.Session, "create", staticmethod(fake_create))
    r = client.post("/billing/checkout")
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.com/")
    assert seen["api_key"] == "sk_test_dummy" and seen["line_items"][0]["price"] == "price_test_lab"
    assert seen["client_reference_id"] == user["id"] and seen["mode"] == "subscription"
    app.state.settings.stripe_secret_key = "sk_live_nope"
    r = client.post("/billing/checkout")
    assert r.status_code == 503 and "non-test" in r.json()["detail"]


def test_webhook_upgrades_and_downgrades(client, user):
    obj = {"id": "cs_test_1", "client_reference_id": user["id"], "customer": "cus_test_1", "subscription": "sub_test_1"}
    r = _post(client, _event("evt_1", "checkout.session.completed", obj))
    assert r.status_code == 200 and r.json()["applied_to"] == user["id"]
    me = client.get("/me").json()
    assert me["plan"] == "lab" and me["usage"]["plan"]["checks_per_month"] == 100
    assert _post(client, _event("evt_1", "checkout.session.completed", obj)).json()["duplicate"]  # idempotent
    r = _post(client, _event("evt_2", "customer.subscription.updated", {"id": "sub_test_1", "customer": "cus_test_1", "status": "past_due"}))
    assert r.json()["applied_to"] == user["id"]
    assert client.get("/me").json()["usage"]["plan"]["name"] == "free"  # lapsed: free limits apply
    _post(client, _event("evt_3", "customer.subscription.deleted", {"id": "sub_test_1", "customer": "cus_test_1"}))
    assert client.get("/me").json()["plan"] == "free"


def test_webhook_rejects_bad_signature_and_live_events(client, user):
    obj = {"client_reference_id": user["id"]}
    assert _post(client, _event("evt_x", "checkout.session.completed", obj), secret="whsec_wrong").status_code == 400
    r = _post(client, _event("evt_y", "checkout.session.completed", obj, livemode=True))
    assert r.status_code == 400 and "live-mode" in r.json()["detail"]
    assert client.get("/me").json()["plan"] == "free"


def test_pubkey_endpoint_matches_signatures(client, settings):
    pk = client.get("/.well-known/receipts-pubkey").json()
    assert pk["alg"] == "Ed25519" and len(base64.b64decode(pk["public_key_b64"])) == 32
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pk["public_key_b64"]))
    signer = signing.load_signer(settings)
    assert signer.key_id == pk["key_id"]
    key.verify(base64.b64decode(signer.sign_hash("ab" * 32)), ("ab" * 32).encode())


def test_signing_key_sources(settings, tmp_path):
    pem = signing.generate_pem()
    a = signing.load_signer(replace(settings, signing_key=pem))
    p = tmp_path / "k.pem"
    p.write_text(pem)
    b = signing.load_signer(replace(settings, signing_key_file=str(p)))
    assert a.key_id == b.key_id
    seed = base64.b64encode(b"\x01" * 32).decode()
    assert signing.load_signer(replace(settings, signing_key=seed)).key_id != a.key_id
    with pytest.raises(ValueError):
        signing.load_signer(replace(settings, signing_key=base64.b64encode(b"short").decode()))
    with pytest.raises(FileNotFoundError):
        signing.load_signer(replace(settings, signing_key_file=str(tmp_path / "none.pem")), create=False)


def test_verify_handles_junk(settings):
    pk = signing.load_signer(settings).public_key
    assert signing.verify("nope", "x", pk)["valid"] is False
    out = signing.verify({"sha256": "00"}, "!!notbase64", pk)
    assert out["hash_ok"] is False and out["signature_ok"] is False


def test_badge_states():
    for state in ("reproduced", "partially_reproduced", "discrepancy_found", "inconclusive", "running", "bogus"):
        svg = badge.render(state)
        assert svg.startswith("<svg") and "receipts" in svg
    assert "#1f9d55" in badge.render("reproduced") and "not found" in badge.render("bogus")
