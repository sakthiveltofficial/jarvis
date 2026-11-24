"""Catalog and in-app downloader for local speech models."""
from __future__ import annotations

import threading
from pathlib import Path

import httpx

from . import config

WHISPER_DIR = config.MODELS_DIR / "whisper"
PIPER_DIR = config.MODELS_DIR / "piper"
KOKORO_DIR = config.MODELS_DIR / "kokoro"

WHISPER_MODELS = {   # name -> approx size MB (int8 CTranslate2 weights from HF)
    "tiny": 75, "tiny.en": 75, "base": 145, "base.en": 145, "small": 484, "small.en": 484,
    "distil-small.en": 330, "medium": 1530, "distil-large-v3": 1510, "large-v3-turbo": 1620,
}

PIPER_VOICES = {   # id -> label
    "en_GB-alan-medium": "Alan (British male) — default, Jarvis-like",
    "en_GB-northern_english_male-medium": "Northern English male",
    "en_US-ryan-high": "Ryan (US male, high quality)",
    "en_US-lessac-medium": "Lessac (US female)",
    "en_GB-jenny_dioco-medium": "Jenny (British female)",
    "en_US-amy-medium": "Amy (US female)",
}

KOKORO_FILES = {
    "int8": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "fp32": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
KOKORO_VOICES = ["bm_george", "bm_lewis", "bm_fable", "am_michael", "am_adam", "af_heart", "bf_emma", "af_bella"]

# download_id -> {"status": queued|downloading|done|error, "progress": 0..1, "error": str}
PROGRESS: dict[str, dict] = {}


def piper_paths(voice: str) -> tuple[Path, Path]:
    return PIPER_DIR / f"{voice}.onnx", PIPER_DIR / f"{voice}.onnx.json"


def piper_url(voice: str, ext: str) -> str:
    lang_region, name, quality = voice.split("-", 2)
    lang = lang_region.split("_")[0]
    return (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            f"{lang}/{lang_region}/{name}/{quality}/{voice}{ext}")


def kokoro_paths(quality: str) -> tuple[Path, Path]:
    return KOKORO_DIR / Path(KOKORO_FILES[quality]).name, KOKORO_DIR / "voices-v1.0.bin"


def whisper_path(name: str) -> Path:
    return WHISPER_DIR / name


def is_installed(kind: str, name: str) -> bool:
    if kind == "whisper":
        return (whisper_path(name) / "model.bin").exists()
    if kind == "piper":
        return all(p.exists() for p in piper_paths(name))
    if kind == "kokoro":
        return all(p.exists() for p in kokoro_paths(name))
    return False


def catalog() -> dict:
    return {
        "whisper": [{"id": n, "size_mb": s, "installed": is_installed("whisper", n)} for n, s in WHISPER_MODELS.items()],
        "piper": [{"id": v, "label": l, "size_mb": 63, "installed": is_installed("piper", v)} for v, l in PIPER_VOICES.items()],
        "kokoro": [{"id": q, "size_mb": 90 if q == "int8" else 310, "installed": is_installed("kokoro", q)} for q in ("int8", "fp32")],
        "kokoro_voices": KOKORO_VOICES,
        "progress": PROGRESS,
    }


def _fetch(url: str, dest: Path, key: str, part: float = 1.0, offset: float = 0.0):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total:
                    PROGRESS[key]["progress"] = offset + part * done / total
    tmp.replace(dest)


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def _download(kind: str, name: str, key: str):
    PROGRESS[key] = {"status": "downloading", "progress": 0.0, "error": ""}
    try:
        if kind == "piper":
            onnx, js = piper_paths(name)
            _fetch(piper_url(name, ".onnx.json"), js, key, 0.02, 0.0)
            _fetch(piper_url(name, ".onnx"), onnx, key, 0.98, 0.02)
        elif kind == "kokoro":
            model, voices = kokoro_paths(name)
            if not voices.exists():
                _fetch(KOKORO_FILES["voices"], voices, key, 0.2, 0.0)
            _fetch(KOKORO_FILES[name], model, key, 0.8, 0.2)
        elif kind == "whisper":
            from faster_whisper import download_model
            out = whisper_path(name)
            expected = WHISPER_MODELS.get(name, 500) * 1_000_000
            stop = threading.Event()

            def watch():
                while not stop.wait(1.0):
                    PROGRESS[key]["progress"] = min(0.99, _dir_size(out) / expected)
            threading.Thread(target=watch, daemon=True).start()
            try:
                download_model(name, output_dir=str(out))
            finally:
                stop.set()
        else:
            raise ValueError(f"unknown model kind {kind}")
        PROGRESS[key] = {"status": "done", "progress": 1.0, "error": ""}
    except Exception as e:  # noqa: BLE001
        PROGRESS[key] = {"status": "error", "progress": 0.0, "error": str(e)}


def start_download(kind: str, name: str) -> str:
    key = f"{kind}:{name}"
    if PROGRESS.get(key, {}).get("status") == "downloading":
        return key
    PROGRESS[key] = {"status": "queued", "progress": 0.0, "error": ""}
    threading.Thread(target=_download, args=(kind, name, key), daemon=True).start()
    return key


def ensure(kind: str, name: str):
    """Download synchronously if missing (used on first use)."""
    if not is_installed(kind, name):
        _download(kind, name, f"{kind}:{name}")
        if not is_installed(kind, name):
            raise RuntimeError(PROGRESS.get(f"{kind}:{name}", {}).get("error") or f"failed to download {kind} {name}")
