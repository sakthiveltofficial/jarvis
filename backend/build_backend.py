"""Freeze the Python backend into a self-contained folder with PyInstaller.

    python build_backend.py            ->  backend/dist/jarvis-backend/jarvis-backend(.exe)

The release workflow bundles that folder into the Tauri installer so end users don't need Python.
"""
import sys

import PyInstaller.__main__

COLLECT_ALL = [
    "faster_whisper", "ctranslate2", "tokenizers", "av", "onnxruntime",
    "piper", "kokoro_onnx", "espeakng_loader", "phonemizer", "language_tags", "segments", "csvw",
    "real_ladybug", "playwright", "sounddevice", "soundfile", "_soundfile_data",
    "mss", "pyautogui", "pynput", "pyperclip", "uvicorn", "anthropic", "openai", "tiktoken",
]

args = ["run_backend.py", "--name", "jarvis-backend", "--onedir", "--noconfirm", "--clean",
        "--paths", ".", "--collect-submodules", "jarvis", "--console"]
for pkg in COLLECT_ALL:
    try:
        __import__(pkg)
    except Exception:
        print(f"[build] skipping {pkg} (not installed)")
        continue
    args += ["--collect-all", pkg]
args += sys.argv[1:]
PyInstaller.__main__.run(args)
