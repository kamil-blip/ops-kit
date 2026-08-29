#!/usr/bin/env python3
"""Local audio transcription via faster-whisper. Free, offline, no API.

Usage:
    transcribe.py <audio_file> [--model small|base|medium|large-v3] [--lang en]

Supports MP3, WAV, M4A, OGG (WhatsApp voice notes), FLAC, etc.
First run downloads the model (~250MB for 'small') to the Hugging Face cache
(%USERPROFILE%\\.cache\\huggingface on Windows, ~/.cache/huggingface elsewhere).
"""
import argparse
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel


def transcribe(audio_path: Path, model_size: str = "small", language: str | None = None) -> str:
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
        beam_size=5,
    )
    print(f"[detected: {info.language} (p={info.language_probability:.2f}), duration={info.duration:.1f}s]", file=sys.stderr)
    parts = []
    for seg in segments:
        line = seg.text.strip()
        parts.append(line)
        print(f"[{seg.start:6.1f}s] {line}", file=sys.stderr)
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe an audio file locally with faster-whisper.")
    ap.add_argument("audio", type=Path, help="Path to audio file (mp3, wav, m4a, ogg, ...)")
    ap.add_argument("--model", default="small", choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--lang", default=None, help="Force language (e.g. 'en'). Auto-detected if omitted.")
    ap.add_argument("--out", type=Path, default=None, help="Write transcript to file instead of stdout.")
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"ERROR: {args.audio} not found", file=sys.stderr)
        return 1

    t0 = time.time()
    text = transcribe(args.audio, model_size=args.model, language=args.lang)
    elapsed = time.time() - t0
    print(f"[done in {elapsed:.1f}s]", file=sys.stderr)

    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
