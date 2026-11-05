import json
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse
import httpx
from .errors import JobError

ID_RE = re.compile(r"[0-9a-f]{24}", re.IGNORECASE)
ALLOWED_HOSTS = {"www.xiaoyuzhoufm.com", "xiaoyuzhoufm.com"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PodcastTranscript/0.1)", "Accept-Encoding": "identity"}


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.next_data = []
        self._in_next_data = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == "meta":
            key = attributes.get("property") or attributes.get("name")
            if key and attributes.get("content"):
                self.meta[key.lower()] = attributes["content"]
        if tag.lower() == "script" and attributes.get("id") == "__NEXT_DATA__":
            self._in_next_data = True

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self._in_next_data = False

    def handle_data(self, data):
        if self._in_next_data:
            self.next_data.append(data)


@dataclass
class Episode:
    episode_id: str
    podcast_id: str | None
    podcast_title: str | None
    title: str
    description: str | None
    shownotes: str | None
    audio_url: str
    mime_type: str | None
    size: int | None
    duration: float | None
    cover_image_url: str | None
    published_at: str | None
    source_url: str
    transcript_media_id: str | None = None

    def as_dict(self):
        return asdict(self)


def normalize_url(raw_url: str) -> tuple[str, str]:
    value = raw_url.strip()
    if ID_RE.fullmatch(value):
        episode_id = value.lower()
    else:
        urls = re.findall(r"https?://[^\s<>\u4e00-\u9fff]+", value)
        value = urls[0].rstrip("）)]}，。.,") if urls else value
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or parsed.netloc.lower() not in ALLOWED_HOSTS:
            raise JobError(
                "INVALID_URL", "请粘贴小宇宙公开单集链接：https://www.xiaoyuzhoufm.com/episode/单集ID"
            )
        match = re.fullmatch(r"/episode/([0-9a-f]{24})/?", parsed.path, re.IGNORECASE)
        if not match:
            raise JobError("INVALID_URL", "请使用单集分享链接，暂不支持播客主页或短链接")
        episode_id = match.group(1).lower()
    return f"https://www.xiaoyuzhoufm.com/episode/{episode_id}", episode_id


def parse_episode(html: str, source_url: str, episode_id: str) -> Episode:
    parser = MetadataParser()
    parser.feed(html)
    episode = {}
    if parser.next_data:
        try:
            payload = json.loads("".join(parser.next_data))
            props = payload.get("props", {}).get("pageProps", {})
            if props.get("statusCode") == 404:
                raise JobError("EPISODE_NOT_FOUND", "节目不存在或已被删除")
            episode = props.get("episode") or {}
        except (json.JSONDecodeError, AttributeError):
            episode = {}
    if episode.get("isPrivateMedia") or episode.get("isPaid") or episode.get("payType") not in (None, "FREE"):
        raise JobError("RESTRICTED_EPISODE", "本版本仅支持公开免费节目")
    media = episode.get("media") or {}
    source = media.get("source") or {}
    if source.get("mode") and source["mode"] != "PUBLIC":
        raise JobError("RESTRICTED_EPISODE", "当前音频需要访问权限")
    audio = (episode.get("enclosure") or {}).get("url") or source.get("url") or parser.meta.get("og:audio")
    if not audio:
        raise JobError("AUDIO_URL_MISSING", "未找到公开音频，节目可能受权限限制或页面结构已变化")
    podcast, image = episode.get("podcast") or {}, episode.get("image") or {}
    transcript = episode.get("transcript")
    transcript_media_id = episode.get("transcriptMediaId")
    if not transcript_media_id and isinstance(transcript, dict):
        transcript_media_id = transcript.get("mediaId")
    return Episode(
        episode_id=episode_id,
        podcast_id=episode.get("pid") or podcast.get("pid"),
        podcast_title=podcast.get("title"),
        title=episode.get("title") or parser.meta.get("og:title") or episode_id,
        description=episode.get("description") or parser.meta.get("og:description"),
        shownotes=episode.get("shownotes"),
        audio_url=audio,
        mime_type=media.get("mimeType"),
        size=media.get("size"),
        duration=episode.get("duration"),
        cover_image_url=image.get("picUrl") or parser.meta.get("og:image"),
        published_at=episode.get("pubDate"),
        source_url=source_url,
        transcript_media_id=transcript_media_id if isinstance(transcript_media_id, str) else None,
    )


def fetch_episode(raw_url: str) -> Episode:
    source_url, episode_id = normalize_url(raw_url)
    try:
        with httpx.Client(timeout=30, follow_redirects=False, headers=HEADERS) as client:
            with client.stream("GET", source_url) as response:
                if response.status_code == 404:
                    raise JobError("EPISODE_NOT_FOUND", "节目不存在或已被删除")
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise JobError("PAGE_TOO_LARGE", "节目页面过大，无法解析")
                    chunks.append(chunk)
        return parse_episode(b"".join(chunks).decode("utf-8"), source_url, episode_id)
    except httpx.HTTPError as exc:
        raise JobError("METADATA_FETCH_FAILED", "获取节目页面失败，请稍后重试") from exc
