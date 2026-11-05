import json
import pytest
from app.errors import JobError
from app.xiaoyuzhou import normalize_url, parse_episode
from conftest import EPISODE_ID, URL


@pytest.mark.parametrize(
    "value",
    [
        URL,
        URL + "?s=share",
        EPISODE_ID,
        "分享一期好节目 " + URL + " 一起来听",
    ],
)
def test_normalize(value):
    assert normalize_url(value) == (URL, EPISODE_ID)


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.test/episode/" + EPISODE_ID,
        "https://www.xiaoyuzhoufm.com.evil.test/episode/" + EPISODE_ID,
        "https://www.xiaoyuzhoufm.com@127.0.0.1/episode/" + EPISODE_ID,
        "https://www.xiaoyuzhoufm.com:8000/episode/" + EPISODE_ID,
        "https://www.xiaoyuzhoufm.com/podcast/" + EPISODE_ID,
        "file:///etc/passwd",
        "abc",
        URL + "/extra",
    ],
)
def test_invalid_input_rejected(value):
    with pytest.raises(JobError, match="链接"):
        normalize_url(value)


def html_for(episode):
    payload = json.dumps({"props": {"pageProps": {"episode": episode}}})
    return f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'


def test_structured_metadata_and_audio():
    episode = parse_episode(
        html_for(
            {
                "title": "中文节目",
                "enclosure": {"url": "https://media.xyzcdn.net/a.m4a"},
                "podcast": {"title": "测试播客"},
                "duration": 320,
            }
        ),
        URL,
        EPISODE_ID,
    )
    assert episode.title == "中文节目"
    assert episode.podcast_title == "测试播客"
    assert episode.duration == 320


def test_og_fallback_and_html_entities():
    episode = parse_episode(
        '<meta property="og:title" content="A &amp; B"><meta property="og:audio" content="https://media.xyzcdn.net/a.mp3">',
        URL,
        EPISODE_ID,
    )
    assert episode.title == "A & B"
    assert episode.audio_url.endswith(".mp3")


@pytest.mark.parametrize(
    "payload",
    [
        {"isPrivateMedia": True},
        {"media": {"source": {"mode": "PRIVATE"}}},
    ],
)
def test_restricted_audio_not_processed(payload):
    with pytest.raises(JobError) as error:
        parse_episode(html_for(payload), URL, EPISODE_ID)
    assert error.value.code == "RESTRICTED_EPISODE"


def test_page_without_audio_has_explicit_error():
    with pytest.raises(JobError) as error:
        parse_episode("<html>changed</html>", URL, EPISODE_ID)
    assert error.value.code == "AUDIO_URL_MISSING"


@pytest.mark.parametrize("metadata", [
    {"transcriptMediaId": "podcast/audio.m4a"},
    {"transcript": {"mediaId": "podcast/audio.m4a"}},
])
def test_transcript_media_id_is_read_from_this_episode_only(metadata):
    metadata["enclosure"] = {"url": "https://media.xyzcdn.net/a.m4a"}
    episode = parse_episode(html_for(metadata), URL, EPISODE_ID)
    assert episode.transcript_media_id == "podcast/audio.m4a"
