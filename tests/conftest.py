import pytest
from app.xiaoyuzhou import Episode

EPISODE_ID = "6a6e3ae21b5e24969ce76363"
URL = f"https://www.xiaoyuzhoufm.com/episode/{EPISODE_ID}"


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
        duration=12,
        cover_image_url=None,
        published_at=None,
        source_url=URL,
        transcript_media_id="podcast-id/audio.m4a",
    )
