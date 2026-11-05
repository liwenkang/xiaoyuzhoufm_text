import io
import wave
import pytest
from app.config import Settings
from app.db import Store
from app.xiaoyuzhou import Episode

EPISODE_ID = "6a6e3ae21b5e24969ce76363"
URL = f"https://www.xiaoyuzhoufm.com/episode/{EPISODE_ID}"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        storage_dir=tmp_path, whisper_model="tiny", transcript_mode="local",
        xiaoyuzhou_access_token="", xiaoyuzhou_token_file=tmp_path / "missing-token.txt",
    )


@pytest.fixture
def store(settings):
    result = Store(settings.db_path)
    result.initialize()
    return result


@pytest.fixture
def episode():
    return Episode(
        episode_id=EPISODE_ID,
        podcast_id=None,
        podcast_title="测试播客",
        title="测试节目",
        description="简介",
        shownotes=None,
        audio_url="https://media.xyzcdn.net/test.wav",
        mime_type="audio/wav",
        size=None,
        duration=1,
        cover_image_url=None,
        published_at=None,
        source_url=URL,
    )


@pytest.fixture
def wav_bytes():
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 16000)
    return result.getvalue()
