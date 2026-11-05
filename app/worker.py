import logging
import threading
import time
from .errors import JobCancelled, JobError, JobInterrupted
from .media import download_audio, probe_audio
from .lock import WorkerLock
from .platform_transcript import fetch_platform_transcript
from .transcriber import FasterWhisperTranscriber, write_transcript
from .xiaoyuzhou import fetch_episode

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.cancelled_jobs = set()
        self.cancel_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="podcast-worker", daemon=True)
        self.transcriber = FasterWhisperTranscriber(settings)
        self.lock = WorkerLock(settings.storage_dir / "worker.lock")

    def start(self):
        self.lock.acquire()
        self.store.recover()
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.lock.release()

    def request_cancel(self, job_id):
        with self.cancel_lock:
            self.cancelled_jobs.add(job_id)
        self.wake_event.set()

    def check_stop(self, job_id=None):
        if self.stop_event.is_set():
            raise JobInterrupted()
        if job_id:
            with self.cancel_lock:
                requested = job_id in self.cancelled_jobs
            if requested or (self.store.get(job_id) or {}).get("status") == "CANCEL_REQUESTED":
                raise JobCancelled()

    def notify(self):
        self.wake_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                job = self.store.claim()
                if job:
                    self.process(job)
                    continue
            except Exception:
                logger.exception("Worker queue error")
            self.wake_event.wait(timeout=1)
            self.wake_event.clear()

    def process(self, job):
        job_id = job["id"]
        directory = self.settings.storage_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)
        files = job.get("files_json") or {}
        def check_job_stop():
            return self.check_stop(job_id)
        try:
            check_job_stop()
            self.store.update(job_id, progress=3, stage="FETCHING_METADATA")
            episode = fetch_episode(job["url"])
            if episode.duration and episode.duration > self.settings.max_duration_seconds:
                raise JobError(
                    "AUDIO_TOO_LONG", f"音频超过 {self.settings.max_duration_seconds // 60} 分钟限制"
                )
            self.store.update(
                job_id, episode_json=episode.as_dict(), transcript_source=None, source_note=None
            )
            mode = job.get("transcript_mode", "local")
            platform = None
            if mode != "local":
                self.store.update(job_id, stage="FETCHING_TRANSCRIPT", progress=5)
                platform = fetch_platform_transcript(episode, self.settings, check_job_stop)
            if platform is not None:
                check_job_stop()
                self.store.update(job_id, stage="GENERATING_FILES", progress=7)
                files.update(write_transcript(
                    platform.segments, directory, episode.as_dict(), "und", None,
                    source="xiaoyuzhou", end_timestamps_inferred=platform.end_timestamps_inferred,
                ))
                # Publish text before optional audio download, so it can be read immediately.
                self.store.update(job_id, files_json=files, transcript_source="xiaoyuzhou")
                if not job.get("download_audio", True):
                    if not self.store.complete(job_id, error_code=None, error_message=None):
                        raise JobCancelled()
                    return
            self.store.update(job_id, stage="DOWNLOADING_AUDIO", progress=8)
            audio = directory / files["audio"] if files.get("audio") else None
            if not audio or not audio.is_file():
                last_download_progress = -1

                def download_progress(done, total):
                    nonlocal last_download_progress
                    progress = 8 + min(30, int(done / total * 30)) if total else 8
                    if progress != last_download_progress:
                        self.store.update(job_id, progress=progress)
                        last_download_progress = progress

                audio = download_audio(episode, directory, self.settings, download_progress, check_job_stop)
                files["audio"] = audio.name
            duration = probe_audio(audio, self.settings.max_duration_seconds)
            episode.duration = duration
            # Publish audio independently: ASR failure must not hide a valid download.
            self.store.update(
                job_id, files_json=files, episode_json=episode.as_dict(), progress=40
            )
            check_job_stop()
            if platform is not None:
                segments, language = platform.segments, "und"
                source, model = "xiaoyuzhou", None
            else:
                self.store.update(job_id, stage="LOADING_MODEL", transcript_source="local")
                self.transcriber.configure(
                    job.get("device") or self.settings.whisper_device,
                    job.get("compute_type") or self.settings.whisper_compute_type,
                )
                self.transcriber.load_model(job["model"])
                check_job_stop()
                self.store.update(job_id, stage="TRANSCRIBING", progress=45)
                last_asr_progress = -1

                def asr_progress(fraction):
                    nonlocal last_asr_progress
                    progress = min(94, 45 + int(fraction * 49))
                    if progress != last_asr_progress:
                        self.store.update(job_id, progress=progress)
                        last_asr_progress = progress

                asr_started = time.perf_counter()
                segments, language = self.transcriber.transcribe(
                    audio,
                    job["language"],
                    duration,
                    episode.title,
                    asr_progress,
                    check_job_stop,
                )
                self.store.update(job_id, asr_seconds=time.perf_counter() - asr_started)
                source, model = "local", job["model"]
            self.store.update(job_id, stage="GENERATING_FILES", progress=96)
            files.update(write_transcript(
                segments, directory, episode.as_dict(), language, model, source=source,
                end_timestamps_inferred=platform.end_timestamps_inferred if platform else False,
            ))
            # Publish generated outputs before the final cancellation check so a stop
            # request never hides files that were already completed on disk.
            self.store.update(job_id, files_json=files, progress=96)
            check_job_stop()
            if not self.store.complete(
                job_id,
                files_json=files,
                transcript_source=source,
                error_code=None,
                error_message=None,
            ):
                raise JobCancelled()
        except JobCancelled:
            self.store.update(job_id, status="CANCELLED", stage="CANCELLED", error_code=None, error_message=None)
        except JobInterrupted:
            self.store.update(job_id, status="QUEUED", stage="QUEUED", progress=0)
        except JobError as exc:
            logger.exception("Task %s failed (%s)", job_id, exc.code)
            self.store.update(
                job_id, status="FAILED", stage="FAILED", error_code=exc.code, error_message=str(exc)
            )
        except Exception:
            logger.exception("Unexpected failure for task %s", job_id)
            self.store.update(
                job_id,
                status="FAILED",
                stage="FAILED",
                error_code="INTERNAL_ERROR",
                error_message="处理失败，请查看服务日志后重试",
            )
        finally:
            with self.cancel_lock:
                self.cancelled_jobs.discard(job_id)
