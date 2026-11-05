"""Audio transcription: the text extractor for recordings.

ffmpeg transcodes a recording to mono 16 kHz MP3 chunks of about
``CHUNK_SECONDS``, each cut at the quietest moment near its boundary so
words are not split. The ``extraction.transcription_model`` tier
transcribes the chunks concurrently with reasoning off. The joined
transcript starts every chunk with its exact ``[hh:mm:ss]`` offset
(model-written timestamps drift) and is cached beside the recording like
OCR text, keyed by the source bytes and :attr:`Transcriber.identity`.

Module-level imports stay lightweight (stdlib, ``psweep.exceptions``):
``document_utils`` imports this module.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess  # noqa: S404 - ffmpeg is a declared pixi dependency
import tempfile
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextvars import copy_context
from pathlib import Path
from typing import Any

from psweep.exceptions import ExtractionError

AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".m4a", ".wav", ".ogg", ".flac"})
CONFIG_KEY = "extraction.transcription_model"
METHOD_PREFIX = "transcript"
# Bump when the prompt, chunking or encoding changes: cached transcripts
# of older versions are re-transcribed.
PROMPT_VERSION = 1
PROMPT = (
    "Transcribe the speech in this audio recording verbatim, in its "
    "original language. Write names exactly as spoken; never invent names "
    "or speaker labels. Mark unintelligible speech as [inaudible]. Output "
    "only the transcript."
)
CHUNK_SECONDS = 300
CUT_WINDOW_SECONDS = 30
ENVELOPE_SECONDS = 0.5
_ENVELOPE_RATE = 8000
_ENCODE = ("-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k")
_RMS_LEVEL = re.compile(r"RMS_level=(-?(?:inf|\d+(?:\.\d+)?))")


class TranscriptionError(RuntimeError):
    """A recording could not be transcribed."""


def is_audio(path: Path) -> bool:
    """Whether ``path`` is a recording (by extension)."""
    return path.suffix.lower() in AUDIO_EXTENSIONS


def _ffmpeg(args: list[str]) -> str:
    """Run ffmpeg and return its log (stderr)."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        message = "ffmpeg not found on PATH; run ParseSweep through pixi, which provides it"
        raise TranscriptionError(message)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [binary, "-nostdin", "-hide_banner", "-nostats", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        lines = result.stderr.strip().splitlines()
        last = lines[-1] if lines else f"exit {result.returncode}"
        message = f"ffmpeg failed: {last}"
        raise TranscriptionError(message)
    return result.stderr


def _envelope(source: Path) -> list[float]:
    """RMS level (dB) of each ``ENVELOPE_SECONDS`` audio window."""
    window = int(_ENVELOPE_RATE * ENVELOPE_SECONDS)
    filters = (
        f"aresample={_ENVELOPE_RATE},asetnsamples=n={window}:p=0,"
        "astats=metadata=1:reset=1,"
        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level"
    )
    log = _ffmpeg(["-i", str(source), "-map", "0:a:0", "-af", filters, "-f", "null", "-"])
    return [float(level) for level in _RMS_LEVEL.findall(log)]


def chunk_starts(levels: list[float]) -> list[float]:
    """Chunk start times (seconds) for an RMS envelope.

    Every ``CHUNK_SECONDS`` the cut moves to the quietest window within
    ``CUT_WINDOW_SECONDS``; the last chunk is 30 s to 6 min long.
    """
    duration = len(levels) * ENVELOPE_SECONDS
    starts = [0.0]
    while duration - starts[-1] > CHUNK_SECONDS + 2 * CUT_WINDOW_SECONDS:
        target = starts[-1] + CHUNK_SECONDS
        lo = int((target - CUT_WINDOW_SECONDS) / ENVELOPE_SECONDS)
        hi = int((target + CUT_WINDOW_SECONDS) / ENVELOPE_SECONDS)
        quietest = min(
            range(lo, hi + 1),
            key=lambda i: (levels[i], abs(i * ENVELOPE_SECONDS - target)),
        )
        starts.append((quietest + 0.5) * ENVELOPE_SECONDS)
    return starts


def _split(source: Path, starts: list[float], workdir: Path) -> list[Path]:
    """Transcode once into one low-bitrate MP3 per chunk."""
    times = ",".join(f"{start:.3f}" for start in starts[1:])
    segment = ["-f", "segment", "-segment_times", times, "-reset_timestamps", "1"] if times else []
    target = workdir / ("chunk%04d.mp3" if times else "chunk0000.mp3")
    _ffmpeg(
        [
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            *_ENCODE,
            *segment,
            str(target),
        ]
    )
    chunks = sorted(workdir.glob("chunk*.mp3"))
    if len(chunks) != len(starts):
        message = f"ffmpeg produced {len(chunks)} chunks for {len(starts)} cuts"
        raise TranscriptionError(message)
    return chunks


def timestamp(seconds: float) -> str:
    """``[hh:mm:ss]`` marker for an offset in seconds."""
    whole = int(seconds)
    return f"[{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}]"


class Transcriber:
    """One transcription tier shared by every document of a run.

    ``max_concurrent`` bounds in-flight transcription requests across
    all documents. The LLM client is created on first use, so fully
    cached runs make no client and no call.
    """

    def __init__(
        self,
        llm: dict[str, Any],
        *,
        max_concurrent: int,
        timeout: int | None = None,
    ):
        self._llm = llm
        self._timeout = timeout
        self._workers = max_concurrent
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._client = None
        self._client_lock = threading.Lock()
        self.model = llm["model"]
        self.identity = f"{METHOD_PREFIX}:{self.model}:v{PROMPT_VERSION}"

    @property
    def usage(self) -> dict[str, float]:
        """Calls, tokens and cost so far (empty when none were sent)."""
        return dict(self._client.usage) if self._client is not None else {}

    def _llm_client(self):
        with self._client_lock:
            if self._client is None:
                from .llm_client import LLMClient

                self._client = LLMClient(**self._llm, timeout=self._timeout)
        return self._client

    def transcribe(self, source: Path, workdir: Path) -> str:
        """Transcribe ``source`` via scratch chunks in ``workdir``."""
        levels = _envelope(source)
        if not levels:
            message = "recording has no decodable audio"
            raise TranscriptionError(message)
        starts = chunk_starts(levels)
        client = self._llm_client()
        workdir.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=workdir, prefix=".chunks-")
        with scratch as tmp:
            chunks = _split(source, starts, Path(tmp))
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                # Chunk workers keep the caller's context, so logs name the recording.
                futures = [
                    pool.submit(copy_context().run, self._transcribe_chunk, client, chunk)
                    for chunk in chunks
                ]
                done, _ = wait(futures, return_when=FIRST_EXCEPTION)
                failure = next((f.exception() for f in done if f.exception()), None)
                if failure is not None:
                    for future in futures:
                        future.cancel()
                    raise failure
        texts = [future.result() for future in futures]
        return "\n\n".join(
            f"{timestamp(start)} {text}" for start, text in zip(starts, texts, strict=True) if text
        )

    def _transcribe_chunk(self, client, chunk: Path) -> str:
        audio = base64.b64encode(chunk.read_bytes()).decode("ascii")
        content = [
            {"type": "text", "text": PROMPT},
            {
                "type": "input_audio",
                "input_audio": {"data": audio, "format": "mp3"},
            },
        ]
        with self._slots:
            try:
                reply = client.complete(
                    [{"role": "user", "content": content}],
                    temperature=0,
                    reasoning_effort="none",
                )
            except ExtractionError as exc:
                error = TranscriptionError(str(exc))
                error.retryable = getattr(exc, "retryable", False)
                raise error from None
        if reply.finish_reason == "length":
            message = "transcription response was truncated"
            raise TranscriptionError(message)
        return (reply.content or "").strip()


def build_transcriber(inputs: dict[str, Any]) -> Transcriber | None:
    """The run's transcriber; None without ``transcription_model``."""
    tier = inputs.get("transcription_model")
    if not tier:
        return None
    from .llm_factory import (
        DEFAULT_MAX_CONCURRENT,
        resolve_llm_kwargs,
    )

    llm = resolve_llm_kwargs(tier, models=inputs.get("models"), llm_config=inputs.get("llm_config"))
    return Transcriber(
        llm,
        max_concurrent=inputs.get("max_concurrent") or DEFAULT_MAX_CONCURRENT,
        timeout=inputs.get("timeout_seconds"),
    )


def transcript(source: Path, transcriber: Transcriber | None) -> str:
    """The recording's transcript: cached, or transcribed and cached.

    Without a transcriber (review, validation, CLI utilities) any cached
    transcript is used; with one, only a transcript of its identity is.
    """
    from .document_utils import (
        TEXT_CACHE_DIRNAME,
        read_text_cache,
        write_text_cache,
    )

    cached = read_text_cache(source, return_meta=True)
    if cached is not None:
        method = str(cached[1].get("method") or "")
        if method == getattr(transcriber, "identity", None) or (
            transcriber is None and method.startswith(f"{METHOD_PREFIX}:")
        ):
            return cached[0]
    if transcriber is None:
        message = (
            f"{source.name} is audio and has no transcript; add "
            f"`{CONFIG_KEY}: <tier>` (a models: alias) to the run config"
        )
        raise TranscriptionError(message)
    text = transcriber.transcribe(source, source.parent / TEXT_CACHE_DIRNAME)
    write_text_cache(source, text, method=transcriber.identity)
    return text
