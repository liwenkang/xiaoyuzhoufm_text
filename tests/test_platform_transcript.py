import json
import socket

import httpx
import pytest

from app import platform_transcript as platform
from app.errors import AppError
from app.platform_transcript import Segment
from conftest import EPISODE_ID


TOKEN = "test-operator-token"
CDN = "https://media.xyzcdn.net/example/transcript.json?signature=test"
HTTP_CLIENT = httpx.Client


def mock_http(monkeypatch, handler):
    monkeypatch.setattr(
        platform.httpx, "Client", lambda **kwargs: HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )


def test_reads_authenticated_address_then_cdn_without_credentials(episode, monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if str(request.url) == platform.API_URL:
            assert request.method == "POST"
            assert request.headers["x-jike-access-token"] == TOKEN
            assert json.loads(request.content) == {"eid": EPISODE_ID, "mediaId": episode.transcript_media_id}
            return httpx.Response(
                200, json={"data": {"transcriptUrl": CDN}}, headers={"set-cookie": "api-secret=test; Domain=.xyzcdn.net"}
            )
        assert "x-jike-access-token" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, json=[{"text": "你好", "startMs": 1500}, {"text": "世界", "startMs": 4000}])

    mock_http(monkeypatch, handle)
    result = platform.fetch_platform_transcript(episode, TOKEN)
    assert result.segments == [Segment(1.5, 4, "你好"), Segment(4, 12, "世界")]
    assert result.end_timestamps_inferred is True
    assert len(requests) == 2


def test_end_times_preserved_and_blank_rows_ignored():
    result = platform.parse_segments([
        {"text": " ", "startMs": 0},
        {"text": "第一段", "startMs": 100, "endMs": 850},
        {"text": "第二段", "startMs": 1000, "endMs": 1900},
    ], 2)
    assert result.segments == [Segment(0.1, 0.85, "第一段"), Segment(1, 1.9, "第二段")]
    assert result.end_timestamps_inferred is False


@pytest.mark.parametrize("payload", [
    [], {"segments": []}, [{"text": "丢时间戳"}],
    [{"text": "错误单位", "start": 0}], [{"text": "坏数据", "startMs": -1}],
    [{"text": "坏数据", "startMs": True}], [{"text": "坏数据", "startMs": float("nan")}],
    [{"text": "倒序", "startMs": 1000}, {"text": "倒序", "startMs": 500}],
    [{"text": "结束错误", "startMs": 1000, "endMs": 1000}],
    [{"text": "超时长", "startMs": 3000}],
    [{"text": "部分有效", "startMs": 0}, {"text": 12, "startMs": 1000}],
])
def test_invalid_transcript_never_silently_becomes_success(payload):
    with pytest.raises(AppError) as error:
        platform.parse_segments(payload, 2)
    assert error.value.code == "PLATFORM_TRANSCRIPT_INVALID"


@pytest.mark.parametrize("status", [401, 403])
def test_expired_token_has_actionable_sanitized_error(episode, monkeypatch, status):
    mock_http(monkeypatch, lambda request: httpx.Response(status, text=TOKEN))
    with pytest.raises(AppError) as error:
        platform.fetch_platform_transcript(episode, TOKEN)
    assert error.value.code == ("PLATFORM_AUTH_EXPIRED" if status == 401 else "PLATFORM_AUTH_FORBIDDEN")
    assert TOKEN not in str(error.value)


def test_api_redirect_is_not_followed(episode, monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    mock_http(monkeypatch, handle)
    with pytest.raises(AppError):
        platform.fetch_platform_transcript(episode, TOKEN)
    assert len(requests) == 1


@pytest.mark.parametrize("url", ["https://evil.test/a", "http://media.xyzcdn.net/a", "https://127.0.0.1/a"])
def test_cdn_address_rejected_before_request(episode, monkeypatch, url):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"transcriptUrl": url}})

    mock_http(monkeypatch, handle)
    with pytest.raises(AppError) as error:
        platform.fetch_platform_transcript(episode, TOKEN)
    assert error.value.code == "PLATFORM_TRANSCRIPT_UNSAFE_URL"
    assert len(requests) == 1


def test_cdn_redirect_revalidated(episode, monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if str(request.url) == platform.API_URL:
            return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})
        return httpx.Response(302, headers={"location": "https://evil.test/a"})

    mock_http(monkeypatch, handle)
    with pytest.raises(AppError) as error:
        platform.fetch_platform_transcript(episode, TOKEN)
    assert error.value.code == "PLATFORM_TRANSCRIPT_UNSAFE_URL"
    assert len(requests) == 2


def test_response_limit_and_network_errors_are_sanitized(episode, monkeypatch):

    def handle(request):
        if str(request.url) == platform.API_URL:
            return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})
        return httpx.Response(200, content=b"x" * 11)

    mock_http(monkeypatch, handle)
    with pytest.raises(AppError) as error:
        platform.fetch_platform_transcript(episode, TOKEN, max_bytes=10)
    assert error.value.code == "PLATFORM_TRANSCRIPT_INVALID"

    def failure(request):
        raise httpx.ReadTimeout(TOKEN + CDN)

    mock_http(monkeypatch, failure)
    with pytest.raises(AppError) as error:
        platform.fetch_platform_transcript(episode, TOKEN, max_bytes=10)
    assert TOKEN not in str(error.value)
    assert CDN not in str(error.value)


def test_cdn_private_dns_is_rejected(episode, monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})

    mock_http(monkeypatch, handle)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(AppError) as error:
        platform.fetch_platform_transcript(episode, TOKEN)
    assert error.value.code == "PLATFORM_TRANSCRIPT_UNSAFE_URL"
    assert len(requests) == 1
