from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.app import create_app
from service.config import ServiceSettings
from service.db import Database
from service.tests import fakes

WEBHOOK_SECRET = "whsec_test_" + "0" * 24  # not a real secret: a local test value


@pytest.fixture
def settings(tmp_path) -> ServiceSettings:
    return ServiceSettings(
        database_url=f"sqlite:///{(tmp_path / 'svc.db').as_posix()}", data_dir=str(tmp_path / "data"),
        public_base_url="http://testserver", secret_key="test-secret", secret_key_from_env=True,
        stripe_secret_key="sk_test_dummy", stripe_webhook_secret=WEBHOOK_SECRET, stripe_price_lab="price_test_lab",
        runner="docker", executor="inline", pipeline_factory="service.tests.fakes:ok_factory")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def db(app) -> Database:
    return app.state.db


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_calls():
    fakes.CALLS.clear()
    yield


def signup(client, email="ada@example.org", password="correct horse"):
    r = client.post("/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def user(client):
    return signup(client)


@pytest.fixture
def api_key(client, user):
    r = client.post("/api-keys", json={"name": "ci"})
    assert r.status_code == 201, r.text
    return r.json()["key"]
