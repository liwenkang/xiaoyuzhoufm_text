import socket
import httpx
import pytest
from app import media
from app.errors import JobError


def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://media.xyzcdn.net/a",
        "https://media.xyzcdn.net.evil.test/a",
        "https://127.0.0.1/a",
        "https://media.xyzcdn.net:8080/a",
        "https://user:pass@media.xyzcdn.net/a",
    ],
)
def test_unsafe_host_rejected(url):
    with pytest.raises(JobError):
        media.validate_audio_url(url, (".xyzcdn.net",))


def test_allowlisted_dns_cannot_resolve_to_private_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(JobError) as error:
        media.validate_audio_url("https://media.xyzcdn.net/a", (".xyzcdn.net",))
    assert error.value.code == "UNSAFE_AUDIO_URL"


def mock_http(monkeypatch, handler):
    original = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(media.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))


def test_real_wav_download_validated_and_atomically_saved(settings, episode, wav_bytes, monkeypatch):
    public_dns(monkeypatch)
    mock_http(
        monkeypatch, lambda req: httpx.Response(200, content=wav_bytes, headers={"content-type": "audio/wav"})
    )
    result = media.download_audio(episode, settings.storage_dir, settings, lambda *args: None, lambda: None)
    assert result.read_bytes() == wav_bytes
    assert not (settings.storage_dir / "audio.part").exists()
    assert media.probe_audio(result, 60) > 0


def test_redirect_cannot_leave_allowlist(settings, episode, monkeypatch):
    public_dns(monkeypatch)
    requests = []

    def handle(req):
        requests.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.test/secret"})

    mock_http(monkeypatch, handle)
    with pytest.raises(JobError) as error:
        media.download_audio(episode, settings.storage_dir, settings, lambda *args: None, lambda: None)
    assert error.value.code == "UNSUPPORTED_AUDIO_HOST"
    assert len(requests) == 1


def test_size_limit_cleans_partial_file(settings, episode, wav_bytes, monkeypatch):
    public_dns(monkeypatch)
    settings.max_audio_bytes = 10
    mock_http(
        monkeypatch, lambda req: httpx.Response(200, content=wav_bytes, headers={"content-type": "audio/wav"})
    )
    with pytest.raises(JobError) as error:
        media.download_audio(episode, settings.storage_dir, settings, lambda *args: None, lambda: None)
    assert error.value.code == "AUDIO_TOO_LARGE"
    assert not (settings.storage_dir / "audio.part").exists()


def test_html_instead_of_audio_rejected(settings, episode, monkeypatch):
    public_dns(monkeypatch)
    mock_http(monkeypatch, lambda req: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(JobError) as error:
        media.download_audio(episode, settings.storage_dir, settings, lambda *args: None, lambda: None)
    assert error.value.code == "INVALID_AUDIO"


def test_real_asr_decoder_accepts_wav(settings, wav_bytes):
    # Covers faster-whisper/PyAV compatibility without downloading a model.
    from faster_whisper.audio import decode_audio

    path = settings.storage_dir / "decoder-test.wav"
    path.write_bytes(wav_bytes)
    audio = decode_audio(str(path), sampling_rate=16000)
    assert audio.ndim == 1
    assert len(audio) == 16000
