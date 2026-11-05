from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.serverless import create_serverless_app
from app.xiaoyuzhou import Episode
from app.transcriber import Segment


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

    def fetch(_episode, _settings, _check_stop, access_token=None):
        received["token"] = access_token
        return result

    monkeypatch.setattr("app.serverless.fetch_platform_transcript", fetch)
    settings = SimpleNamespace(xiaoyuzhou_access_token="token")

    with TestClient(create_serverless_app(settings)) as client:
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
    settings = SimpleNamespace(xiaoyuzhou_access_token="token")
    with TestClient(create_serverless_app(settings)) as client:
        response = client.post(
            "/api/transcript",
            json={"url": "a" * 24},
            headers={"Origin": "https://evil.example", "Host": "testserver"},
        )
    assert response.status_code == 403


def test_transcript_endpoint_requires_token():
    settings = SimpleNamespace(xiaoyuzhou_access_token="")
    with TestClient(create_serverless_app(settings)) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24})
    assert response.status_code == 422
    assert "access token" in response.json()["detail"]
