from fastapi.testclient import TestClient

from service import security
from service.tests.conftest import signup


def test_password_hash_roundtrip():
    h = security.hash_password("hunter2hunter2")
    assert h.startswith("scrypt$") and "hunter2" not in h
    assert security.verify_password("hunter2hunter2", h)
    assert not security.verify_password("wrong-password", h)
    assert not security.verify_password("x", None)
    assert not security.verify_password("x", "garbage")


def test_session_cookie_tamper_and_expiry():
    c = security.sign_session("u1", "k", now=1000)
    assert security.read_session(c, "k", now=1001) == "u1"
    assert security.read_session(c, "other", now=1001) is None
    assert security.read_session(c.replace("u1", "u2"), "k", now=1001) is None
    assert security.read_session(c, "k", now=1000 + security.SESSION_TTL_S + 1) is None


def test_signup_login_me(client, app):
    u = signup(client)
    assert u["plan"] == "free"
    assert client.get("/me").json()["email"] == "ada@example.org"
    assert client.post("/auth/signup", json={"email": "ADA@example.org", "password": "another pw"}).status_code == 409
    assert client.post("/auth/signup", json={"email": "nope", "password": "longenough"}).status_code == 422
    assert client.post("/auth/signup", json={"email": "b@example.org", "password": "short"}).status_code == 422
    with TestClient(app) as anon:
        assert anon.get("/me").status_code == 401
        assert anon.post("/auth/login", json={"email": "ada@example.org", "password": "bad bad bad"}).status_code == 401
        r = anon.post("/auth/login", json={"email": "ada@example.org", "password": "correct horse"})
        assert r.status_code == 200 and anon.get("/me").status_code == 200


def test_api_keys_create_use_revoke(client, app, api_key):
    assert api_key.startswith("rcpt_")
    with TestClient(app) as anon:
        h = {"Authorization": f"Bearer {api_key}"}
        assert anon.get("/me", headers=h).status_code == 200
        assert anon.get("/me", headers={"X-API-Key": api_key}).status_code == 200
        assert anon.get("/me", headers={"Authorization": "Bearer rcpt_wrong"}).status_code == 401
    keys = client.get("/api-keys").json()
    assert len(keys) == 1 and "key" not in keys[0] and api_key.startswith(keys[0]["prefix"])
    assert client.delete(f"/api-keys/{keys[0]['id']}").json()["revoked"]
    with TestClient(app) as anon:
        assert anon.get("/me", headers={"Authorization": f"Bearer {api_key}"}).status_code == 401


def test_magic_link_stub(app):
    with TestClient(app) as c:
        r = c.post("/auth/magic-link", json={"email": "grace@example.org"})
        link = r.json()["dev_link"]
        path = link.replace("http://testserver", "")
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/me").json()["email"] == "grace@example.org"
        assert c.get(path, follow_redirects=False).status_code == 400  # single use


def test_html_pages_render(client, app):
    with TestClient(app) as anon:
        assert "Independent reproduction" in anon.get("/").text
        assert "Create an account" in anon.get("/login?mode=signup").text
        r = anon.post("/auth/signup", data={"email": "web@example.org", "password": "webpassword"}, follow_redirects=False)
        assert r.status_code == 303
        page = anon.get("/app").text
        assert "Check a paper" in page and "Upgrade to Lab" in page
