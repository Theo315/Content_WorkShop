"""Public deployment still requires private storage and signed file capabilities."""
import json

import pytest
from fastapi.testclient import TestClient

from backend import settings
from backend.app import app


@pytest.fixture
def public_client(monkeypatch):
    monkeypatch.setattr(settings, "IS_VERCEL", True)
    monkeypatch.setenv("APP_PUBLIC_ACCESS", "true")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-signing-secret-for-public-mode-only")
    for name in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "test-not-real-credentials")
    # These tests use endpoints that never read/write storage.
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def test_anonymous_access_without_login_or_cookie(public_client, monkeypatch):
    for password in ("", "short-ignored-password", "old-password-from-private-deployment"):
        monkeypatch.setenv("APP_PASSWORD", password)
        status = public_client.get("/api/status").json()
        assert status["ready"] and status["authenticated"]
        assert status["auth_required"] is False and status["storage"] == "s3"
        response = public_client.post("/api/png/colors", json={"text": "A #123456"})
        assert response.status_code == 200, response.text
        assert response.json()["colors"][0]["hex"] == "#123456"
        assert not public_client.cookies


def test_public_login_does_not_create_session_cookie(public_client):
    response = public_client.post("/api/session", json={"password": "irrelevant"})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("missing", ["SESSION_SECRET", "S3_BUCKET"])
def test_public_mode_still_fails_closed_when_misconfigured(public_client, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert public_client.get("/api/status").json()["ready"] is False
    assert public_client.post("/api/png/colors", json={"text": "A #123456"}).status_code == 503


def test_public_mode_retains_origin_and_file_token_checks(public_client):
    response = public_client.post("/api/png/colors", json={"text": "A #123456"},
                                  headers={"Origin": "https://unrelated.example"})
    assert response.status_code == 403
    response = public_client.post("/api/files/refresh", json={"token": "forged-file-token"})
    assert response.status_code == 400


def test_private_mode_still_requires_password_and_session(public_client, monkeypatch):
    monkeypatch.delenv("APP_PUBLIC_ACCESS")
    assert public_client.get("/api/status").json()["ready"] is False
    monkeypatch.setenv("APP_PASSWORD", "private-test-password-at-least-16")
    status = public_client.get("/api/status").json()
    assert status["ready"] and status["auth_required"] and not status["authenticated"]
    assert public_client.post("/api/png/colors", json={"text": "A #123456"}).status_code == 401
    assert public_client.post("/api/session", json={"password": "private-test-password-at-least-16"}).status_code == 200
    assert public_client.post("/api/png/colors", json={"text": "A #123456"}).status_code == 200


@pytest.mark.parametrize("value,enabled", [("", False), ("false", False), ("0", False),
                                          ("off", False), ("yes", False), ("true", True),
                                          ("1", True), (" TRUE ", True)])
def test_public_access_requires_explicit_flag(monkeypatch, value, enabled):
    monkeypatch.setenv("APP_PUBLIC_ACCESS", value)
    assert settings.public_access() is enabled


def test_vercel_deployment_explicitly_enables_public_access():
    config = json.loads((settings.ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["env"]["APP_PUBLIC_ACCESS"] == "true"
