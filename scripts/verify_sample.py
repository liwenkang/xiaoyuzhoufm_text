"""Optional real-network smoke check: download a public episode and transcribe a short excerpt."""

import argparse
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.media import download_audio, probe_audio
from app.transcriber import FasterWhisperTranscriber, write_transcript
from app.xiaoyuzhou import fetch_episode


def extract_excerpt(source, target, seconds):
    import av

    count = 0
    limit = int(seconds * 16000)
    with av.open(str(source)) as container, wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                data = converted.to_ndarray().tobytes()[: max(0, limit - count) * 2]
                output.writeframes(data)
                count += len(data) // 2
            if count >= limit:
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be between 1 and 300")
    settings = Settings()
    print("1/4 获取真实节目信息", flush=True)
    episode = fetch_episode(args.url)
    directory = settings.storage_dir / "verification" / episode.episode_id
    directory.mkdir(parents=True, exist_ok=True)
    print(f"{episode.title} / {episode.duration} 秒", flush=True)
    existing = list(directory.glob("audio.*"))
    existing = [path for path in existing if path.suffix != ".part"]
    print("2/4 下载并校验完整原始音频", flush=True)
    audio = (
        existing[0]
        if existing
        else download_audio(episode, directory, settings, lambda *args: None, lambda: None)
    )
    full_duration = probe_audio(audio, settings.max_duration_seconds)
    excerpt = directory / "excerpt.wav"
    extract_excerpt(audio, excerpt, args.seconds)
    print("3/4 加载真实语音模型", flush=True)
    transcriber = FasterWhisperTranscriber(settings)
    transcriber.load_model(settings.whisper_model)
    print(f"4/4 转写前 {args.seconds} 秒（不是整集）", flush=True)
    excerpt_duration = probe_audio(excerpt, settings.max_duration_seconds)
    segments, language = transcriber.transcribe(
        excerpt, "zh", excerpt_duration, episode.title, lambda *args: None, lambda: None
    )
    metadata = episode.as_dict()
    metadata["title"] += f"（前 {args.seconds} 秒验证片段）"
    metadata["duration"] = excerpt_duration
    files = write_transcript(segments, directory, metadata, language, settings.whisper_model)
    report = {
        "source_url": episode.source_url,
        "audio_bytes": audio.stat().st_size,
        "full_duration_seconds": full_duration,
        "verified_excerpt_seconds": excerpt_duration,
        "model": settings.whisper_model,
        "segments": len(segments),
        "files": files,
    }
    (directory / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print("\n".join(segment.text for segment in segments), flush=True)


if __name__ == "__main__":
    main()
