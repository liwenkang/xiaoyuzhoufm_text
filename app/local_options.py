"""Per-job ASR configuration and deliberately approximate timing estimates."""

import os
from functools import lru_cache
from statistics import median

from .errors import JobError

MODELS = {
    "tiny": "Tiny · 快速预览",
    "base": "Base · 轻量",
    "small": "Small · 均衡",
    "medium": "Medium · 更精细",
    "large-v3": "Large v3 · 大模型",
}
COMPUTE_TYPES = ("auto", "int8", "float32", "float16", "int8_float16")


@lru_cache(maxsize=1)
def runtime_capabilities():
    import ctranslate2

    devices = {}
    for device in ("cpu", "cuda"):
        try:
            if device == "cuda" and not ctranslate2.get_cuda_device_count():
                continue
            devices[device] = sorted(ctranslate2.get_supported_compute_types(device))
        except (RuntimeError, ValueError):
            continue
    return devices


def resolve_config(settings, model=None, device=None, compute_type=None):
    model = model or settings.whisper_model
    if model not in MODELS and model != settings.whisper_model:
        raise JobError("LOCAL_CONFIG_INVALID", "不支持该识别模型，请从列表中选择。")
    device = device or settings.whisper_device
    compute_type = compute_type or settings.whisper_compute_type
    if device not in {"auto", "cpu", "cuda"} or compute_type not in COMPUTE_TYPES:
        raise JobError("LOCAL_CONFIG_INVALID", "识别设备或计算精度不受支持。")
    capabilities = runtime_capabilities()
    if device == "auto":
        device = "cuda" if "cuda" in capabilities else "cpu"
    if device not in capabilities:
        raise JobError("LOCAL_CONFIG_INVALID", "当前设备不可用，请选择本机支持的识别设备。")
    if compute_type == "auto":
        preferences = ("float16", "int8_float16", "int8", "float32") if device == "cuda" else ("int8", "float32")
        compute_type = next((kind for kind in preferences if kind in capabilities[device]), "float32")
    if compute_type not in capabilities[device]:
        raise JobError("LOCAL_CONFIG_INVALID", "当前设备不支持所选计算精度，请选择自动或其他精度。")
    return {"model": model, "device": device, "compute_type": compute_type}


def local_options(settings):
    models = [{"value": key, "label": label} for key, label in MODELS.items()]
    if settings.whisper_model not in MODELS:
        models.append({"value": settings.whisper_model, "label": "服务端自定义模型"})
    return {
        "models": models,
        "devices": runtime_capabilities(),
        "defaults": {
            "model": settings.whisper_model, "device": settings.whisper_device,
            "compute_type": settings.whisper_compute_type,
        },
    }


def estimate_asr(store, config, duration):
    samples = store.asr_timings(**config)
    if samples:
        ratio = median(samples)
        lower, upper = ratio * 0.7, ratio * 1.5
        basis = "history"
    else:
        # Broad starting ranges, not measurements of this computer. The published
        # faster-whisper benchmark is only a reference; hardware and speech vary.
        ranges = {
            "tiny": (0.04, 0.5), "base": (0.08, 0.8), "small": (0.15, 1.5),
            "medium": (0.4, 4), "large-v3": (0.8, 8),
        }
        lower, upper = ranges.get(config["model"], (0.1, 8))
        scale = max(1, 8 / max(1, min(8, os.cpu_count() or 1)))
        if config["device"] == "cuda":
            scale = 0.2
        elif config["compute_type"] == "float32":
            scale *= 1.5
        lower, upper = lower * scale, upper * scale
        basis = "rough"
    return {
        "min_seconds": max(1, round(duration * lower)),
        "max_seconds": max(2, round(duration * upper)),
        "basis": basis, "sample_count": len(samples),
    }
