import pytest
from fastapi.testclient import TestClient

from app import platform_transcript as platform
from app.config import Settings
from app.main import SaveTokenRequest, create_app

TOKEN = "test-page-access-token"


def test_save_token_creates_directory_persists_and_does_not_expose_value(settings):
    settings.xiaoyuzhou_token_file = settings.storage_dir / "secrets" / "token.txt"
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/platform/token", json={"token": f"  {TOKEN}\n"})
        assert response.status_code == 200
        assert response.json()["status"] == "saved"
        assert response.headers["cache-control"] == "no-store"
        assert TOKEN not in response.text
        assert TOKEN not in repr(SaveTokenRequest(token=TOKEN))
        assert platform.read_access_token(settings) == TOKEN
        assert list(settings.xiaoyuzhou_token_file.parent.iterdir()) == [settings.xiaoyuzhou_token_file]
        assert client.get("/api/health").json()["platform_credentials_configured"] is True
        assert TOKEN not in client.get("/api/health").text
        assert client.post("/api/platform/check-token", json={}).json()["status"] == "unchecked"
        assert client.get("/api/jobs").json() == []
        assert client.get("/api/platform/token").status_code == 405
    # A new settings instance still reads the saved file after a restart.
    fresh = Settings(
        storage_dir=settings.storage_dir, xiaoyuzhou_access_token="",
        xiaoyuzhou_token_file=settings.xiaoyuzhou_token_file,
    )
    assert platform.read_access_token(fresh) == TOKEN


@pytest.mark.parametrize("token", ["", "  \n ", "bad\nheader", "Bearer secret", "中文", "a" * 16385])
def test_invalid_input_keeps_existing_token_and_returns_no_secret(settings, token):
    settings.xiaoyuzhou_token_file.write_text(TOKEN, encoding="utf-8")
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/platform/token", json={"token": token})
        assert response.status_code == 400
        assert response.json()["error_code"] == "PLATFORM_AUTH_INVALID"
        assert platform.read_access_token(settings) == TOKEN
        assert TOKEN not in response.text
        assert "bad" not in response.text
        assert "Bearer secret" not in response.text


@pytest.mark.parametrize("payload", [{}, {"token": None}, {"token": [TOKEN]}, {"token": {"value": TOKEN}}])
def test_schema_errors_never_echo_credentials(settings, payload):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/platform/token", json=payload)
        assert response.status_code == 422
        assert TOKEN not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert not settings.xiaoyuzhou_token_file.exists()


def test_bad_json_never_echoes_credentials(settings):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post(
            "/api/platform/token", content='{"token":"' + TOKEN,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert TOKEN not in response.text


def test_save_failure_preserves_existing_token(settings, monkeypatch):
    settings.xiaoyuzhou_token_file.write_text(TOKEN, encoding="utf-8")

    def fail(*args):
        raise PermissionError("secret-should-not-be-reflected")

    monkeypatch.setattr(platform.os, "replace", fail)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/platform/token", json={"token": "replacement-token"})
        assert response.status_code == 400
        assert response.json()["error_code"] == "PLATFORM_AUTH_SAVE_FAILED"
        assert "secret-should-not-be-reflected" not in response.text
        assert platform.read_access_token(settings) == TOKEN
        assert not list(settings.xiaoyuzhou_token_file.parent.glob(".*.tmp"))


def test_env_credentials_are_not_silently_overridden(settings):
    settings.xiaoyuzhou_access_token = TOKEN
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/platform/token", json={"token": "replacement-token"})
        assert response.status_code == 409
        assert response.json()["error_code"] == "PLATFORM_AUTH_ENV_OVERRIDE"
        assert TOKEN not in response.text
        assert not settings.xiaoyuzhou_token_file.exists()
        assert platform.read_access_token(settings) == TOKEN


def test_save_rejects_cross_site_requests(settings):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post(
            "/api/platform/token", json={"token": TOKEN}, headers={"Origin": "https://evil.test"},
        )
        assert response.status_code == 403
        assert not settings.xiaoyuzhou_token_file.exists()


def test_saved_token_is_used_by_next_preflight(settings, episode, monkeypatch):
    episode.transcript_media_id = "test/audio.m4a"
    monkeypatch.setattr("app.xiaoyuzhou.fetch_episode", lambda url: episode)
    received = []

    def fetch_address(episode, token, check_stop):
        received.append(token)
        return "https://media.xyzcdn.net/transcript.json"

    monkeypatch.setattr(platform, "_fetch_transcript_url", fetch_address)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        client.post("/api/platform/token", json={"token": TOKEN})
        response = client.post("/api/platform/check-token", json={"url": episode.source_url})
        assert response.json()["status"] == "valid"
        assert received == [TOKEN]


def test_max_length_token_can_be_read_after_saving(settings):
    platform.save_access_token(settings, "a" * 16384)
    assert len(platform.read_access_token(settings)) == 16384
