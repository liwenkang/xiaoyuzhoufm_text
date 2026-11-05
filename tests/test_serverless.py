from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.errors import AppError
from app.serverless import create_serverless_app
from app.xiaoyuzhou import Episode
from app.platform_transcript import Segment


def test_transcript_endpoint_returns_platform_segments(monkeypatch):
    episode = Episode(
        episode_id="a" * 24,
        podcast_id="podcast",
        podcast_title="播客",
        title="节目",
        description="简介",
        shownotes="shownotes",
        audio_url="https://cdn.example.test/audio.mp3",
        mime_type="audio/mpeg",
        size=10,
        duration=20,
        cover_image_url="https://cdn.example.test/cover.jpg",
        published_at="2026-01-01T00:00:00Z",
        source_url="https://www.xiaoyuzhoufm.com/episode/" + "a" * 24,
        transcript_media_id="media",
    )
    result = SimpleNamespace(
        end_timestamps_inferred=True,
        segments=[Segment(0, 2, "第一段")],
    )
    monkeypatch.setattr("app.serverless.fetch_episode", lambda _url: episode)
    received = {}

    def fetch(_episode, access_token):
        received["token"] = access_token
        return result

    monkeypatch.setattr("app.serverless.fetch_platform_transcript", fetch)

    with TestClient(create_serverless_app()) as client:
        response = client.post("/api/transcript", json={"url": episode.source_url, "token": "user-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["transcript_source"] == "xiaoyuzhou"
    assert body["end_timestamps_inferred"] is True
    assert body["segments"] == [{"start": 0.0, "end": 2.0, "text": "第一段", "speaker": None}]
    assert received["token"] == "user-token"
    assert "audio_url" not in body["episode"]
    assert "transcript_media_id" not in body["episode"]


def test_transcript_endpoint_rejects_cross_origin_post():
    with TestClient(create_serverless_app()) as client:
        response = client.post(
            "/api/transcript",
            json={"url": "a" * 24},
            headers={"Origin": "https://evil.example", "Host": "testserver"},
        )
    assert response.status_code == 403


def test_transcript_endpoint_requires_token():
    with TestClient(create_serverless_app()) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24})
    assert response.status_code == 422
    assert "access token" in response.json()["detail"]


def test_public_files_and_health_share_the_current_entrypoint():
    with TestClient(create_serverless_app()) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "不要把 token 发给别人，token 失效后重新获取并替换即可。" in home.text
        for name in ("app.js", "style.css"):
            assert client.get(f"/static/{name}").status_code == 200
        health = client.get("/api/health")
        assert health.json() == {
            "status": "ok", "transcript_mode": "platform", "auth_mode": "user_supplied",
        }
        assert health.headers["cache-control"] == "no-store"
        for path in ("/api/jobs", "/api/local/options", "/api/platform/token"):
            assert client.get(path).status_code == 404


@pytest.mark.parametrize("token", [None, {"secret": "do-not-echo"}, "", "  ", "bad\nheader", "非ASCII"])
def test_invalid_token_is_rejected_without_network_or_credential_echo(monkeypatch, token):
    def unexpected(_url):
        pytest.fail("Invalid tokens must be rejected before upstream requests")

    monkeypatch.setattr("app.serverless.fetch_episode", unexpected)
    with TestClient(create_serverless_app()) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24, "token": token})
    assert response.status_code in {400, 422}
    assert "do-not-echo" not in response.text
    assert "bad" not in response.text
    assert "非ASCII" not in response.text


@pytest.mark.parametrize(("code", "status"), [
    ("PLATFORM_AUTH_EXPIRED", 401), ("PLATFORM_AUTH_FORBIDDEN", 403),
    ("PLATFORM_TRANSCRIPT_MISSING", 400),
])
def test_platform_errors_keep_their_status_and_disable_caching(monkeypatch, episode, code, status):
    monkeypatch.setattr("app.serverless.fetch_episode", lambda _url: episode)

    def fail(_episode, _token):
        raise AppError(code, "请重新检查平台文稿访问权限。")

    monkeypatch.setattr("app.serverless.fetch_platform_transcript", fail)
    with TestClient(create_serverless_app()) as client:
        response = client.post("/api/transcript", json={"url": episode.source_url, "token": "user-secret"})
    assert response.status_code == status
    assert response.json()["error_code"] == code
    assert "user-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"
