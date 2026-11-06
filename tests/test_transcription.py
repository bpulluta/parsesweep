"""Audio transcription: chunking, LLM accounting, cache, config, pipeline."""

from __future__ import annotations

import base64
import json
import random
import shutil
import subprocess  # noqa: S404 - builds a test recording
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
import yaml

from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.constants import DOCUMENT_EXTENSIONS, MIME_TO_EXTENSION
from psweep.discovery.engine import DiscoveryEngine
from psweep.extraction import document_utils, transcription
from psweep.extraction.document_utils import (
    extract_text_from_document,
    is_supported_document,
    read_text_cache,
)
from psweep import pipeline
from psweep.extraction.llm_client import LLMClient
from psweep.extraction.transcription import (
    AUDIO_EXTENSIONS,
    CHUNK_SECONDS,
    CUT_WINDOW_SECONDS,
    ENVELOPE_SECONDS,
    Transcriber,
    TranscriptionError,
    build_transcriber,
    chunk_starts,
    timestamp,
    transcript,
)
from psweep.utils.log_context import current_document, document_scope

BASE_URL = "https://proxy.example.invalid/v1"
PRICING = "psweep.extraction.llm_client."
LLM = {
    "model": "gemini-3.5-flash",
    "api_key": "k",
    "provider": "openai",
    "base_url": BASE_URL,
}


def _response(text="hello", finish="stop", usage=(1500, 170)):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


def _levels(seconds: float, level: float = -20.0) -> list[float]:
    return [level] * int(seconds / ENVELOPE_SECONDS)


@pytest.fixture
def priced(monkeypatch):
    """Price every response at $0.01 so accounting is deterministic."""
    monkeypatch.setattr(PRICING + "completion_cost", lambda **_: 0.01)


@pytest.fixture
def chunked(monkeypatch):
    """Replace ffmpeg: an envelope of ``seconds`` and one file per chunk."""

    def install(seconds: float):
        monkeypatch.setattr(transcription, "_envelope", lambda _src: _levels(seconds))

        def split(_source, starts, workdir):
            paths = []
            for index in range(len(starts)):
                path = workdir / f"chunk{index:04d}.mp3"
                path.write_bytes(f"chunk-{index}".encode())
                paths.append(path)
            return paths

        monkeypatch.setattr(transcription, "_split", split)

    return install


def _chunk_name(params) -> str:
    part = params["messages"][0]["content"][1]["input_audio"]["data"]
    return base64.b64decode(part).decode()


# ---------------------------------------------------------------- chunking


def test_timestamp_formats_hours_minutes_seconds():
    assert timestamp(0) == "[00:00:00]"
    assert timestamp(309.75) == "[00:05:09]"
    assert timestamp(3 * 3600 + 62) == "[03:01:02]"


@pytest.mark.parametrize("seconds", [1, 200, CHUNK_SECONDS + 60])
def test_short_recordings_are_one_chunk(seconds):
    assert chunk_starts(_levels(seconds)) == [0.0]


def test_cut_moves_to_quietest_window_near_target():
    levels = _levels(700)
    quiet = int(318 / ENVELOPE_SECONDS)
    levels[quiet] = -60.0
    assert chunk_starts(levels)[1] == pytest.approx(318.25)


def test_uniform_audio_is_cut_at_the_target():
    assert chunk_starts(_levels(700))[1] == pytest.approx(CHUNK_SECONDS + 0.25)


def test_silence_outside_the_cut_window_is_ignored():
    levels = _levels(700)
    levels[int(250 / ENVELOPE_SECONDS)] = -90.0
    assert chunk_starts(levels)[1] == pytest.approx(CHUNK_SECONDS + 0.25)


def test_every_chunk_stays_within_bounds():
    rng = random.Random(7)  # noqa: S311 - reproducible levels
    for seconds in [361, 659, 660, 661, 1000, 3 * 3600 + 17]:
        levels = [rng.uniform(-70, -10) for _ in _levels(seconds)]
        starts = chunk_starts(levels)
        ends = [*starts[1:], len(levels) * ENVELOPE_SECONDS]
        lengths = [b - a for a, b in zip(starts, ends, strict=True)]
        low = CHUNK_SECONDS - CUT_WINDOW_SECONDS
        high = CHUNK_SECONDS + CUT_WINDOW_SECONDS
        assert all(low <= length <= high + 1 for length in lengths[:-1])
        last = CHUNK_SECONDS + 2 * CUT_WINDOW_SECONDS
        assert CUT_WINDOW_SECONDS <= lengths[-1] <= last


# ------------------------------------------------------------------ ffmpeg


def test_missing_ffmpeg_names_the_fix(monkeypatch):
    monkeypatch.setattr(transcription.shutil, "which", lambda _: None)
    with pytest.raises(TranscriptionError, match=r"ffmpeg not found.*pixi"):
        transcription._ffmpeg(["-version"])


def test_ffmpeg_failure_reports_last_log_line(monkeypatch):
    monkeypatch.setattr(transcription.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(
        transcription.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stderr="noise\nx.mp3: Invalid data found\n"
        ),
    )
    with pytest.raises(TranscriptionError, match=r"x\.mp3: Invalid data"):
        transcription._ffmpeg(["-i", "x.mp3"])


def test_envelope_parses_rms_levels(monkeypatch):
    log = (
        "[Parsed_ametadata_3] lavfi.astats.Overall.RMS_level=-23.5\n"
        "[Parsed_ametadata_3] lavfi.astats.Overall.RMS_level=-inf\n"
        "[Parsed_ametadata_3] lavfi.astats.Overall.RMS_level=-7\n"
    )
    monkeypatch.setattr(transcription, "_ffmpeg", lambda _args: log)
    assert transcription._envelope(Path("a.mp3")) == [-23.5, float("-inf"), -7]


def test_split_transcodes_once_at_the_cut_points(monkeypatch, tmp_path):
    calls = []

    def ffmpeg(args):
        calls.append(args)
        for index in range(3):
            (tmp_path / f"chunk{index:04d}.mp3").write_bytes(b"x")
        return ""

    monkeypatch.setattr(transcription, "_ffmpeg", ffmpeg)
    starts = [0.0, 300.25, 610.5]
    chunks = transcription._split(Path("a.mp3"), starts, tmp_path)
    assert [path.name for path in chunks] == [
        "chunk0000.mp3",
        "chunk0001.mp3",
        "chunk0002.mp3",
    ]
    (args,) = calls
    assert args[args.index("-segment_times") + 1] == "300.250,610.500"
    assert args[args.index("-ac") + 1] == "1"
    assert args[args.index("-ar") + 1] == "16000"


def test_split_rejects_a_chunk_count_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(transcription, "_ffmpeg", lambda _args: "")
    (tmp_path / "chunk0000.mp3").write_bytes(b"x")
    with pytest.raises(TranscriptionError, match="1 chunks for 2 cuts"):
        transcription._split(Path("a.mp3"), [0.0, 300.0], tmp_path)


# -------------------------------------------------------------- transcriber


def test_chunks_are_sent_as_audio_with_reasoning_off(monkeypatch, tmp_path, chunked, priced):
    chunked(650)
    sent = []

    def complete(_self, params):
        sent.append(params)
        return _response(f" text of {_chunk_name(params)} ")

    monkeypatch.setattr(LLMClient, "_complete", complete)
    text = Transcriber(LLM, max_concurrent=2).transcribe(tmp_path / "a.mp3", tmp_path / ".text")
    assert text == ("[00:00:00] text of chunk-0\n\n[00:05:00] text of chunk-1")
    params = sent[0]
    assert params["model"] == "openai/gemini-3.5-flash"
    assert params["base_url"] == BASE_URL
    assert params["temperature"] == 0
    assert params["reasoning_effort"] == "none"
    text_part, audio_part = params["messages"][0]["content"]
    assert text_part == {"type": "text", "text": transcription.PROMPT}
    assert audio_part["input_audio"]["format"] == "mp3"
    assert not list((tmp_path / ".text").glob(".chunks-*"))


def test_transcriber_names_its_model():
    assert Transcriber(LLM, max_concurrent=1).model == "gemini-3.5-flash"


def test_usage_is_accounted_like_extraction(monkeypatch, tmp_path, chunked, priced):
    chunked(650)
    monkeypatch.setattr(LLMClient, "_complete", lambda *_: _response())
    transcriber = Transcriber(LLM, max_concurrent=2)
    assert transcriber.usage == {}
    transcriber.transcribe(tmp_path / "a.mp3", tmp_path)
    assert transcriber.usage == {
        "calls": 2,
        "cost": pytest.approx(0.02),
        "input_tokens": 3000,
        "output_tokens": 340,
        "unknown_cost_calls": 0,
    }


def test_proxy_routes_are_priced_as_the_configured_model(monkeypatch, tmp_path, chunked):
    def unmapped(**_):
        raise litellm.exceptions.NotFoundError("unmapped", "openai", "m")

    seen = {}

    def per_token(*, model, usage_object):
        seen["model"] = model
        return 0.003, 0.001

    monkeypatch.setattr(PRICING + "completion_cost", unmapped)
    monkeypatch.setattr(PRICING + "cost_per_token", per_token)
    chunked(60)
    monkeypatch.setattr(LLMClient, "_complete", lambda *_: _response())
    transcriber = Transcriber(LLM, max_concurrent=1)
    transcriber.transcribe(tmp_path / "a.mp3", tmp_path)
    assert seen["model"] == "gemini-3.5-flash"
    assert transcriber.usage["cost"] == pytest.approx(0.004)
    assert transcriber.usage["unknown_cost_calls"] == 0


def test_unpriced_models_count_as_unknown_cost(monkeypatch, tmp_path, chunked):
    def unpriced(**_):
        raise ValueError("no price")

    monkeypatch.setattr(PRICING + "completion_cost", unpriced)
    monkeypatch.setattr(PRICING + "cost_per_token", unpriced)
    chunked(60)
    monkeypatch.setattr(LLMClient, "_complete", lambda *_: _response())
    transcriber = Transcriber(LLM, max_concurrent=1)
    transcriber.transcribe(tmp_path / "a.mp3", tmp_path)
    assert transcriber.usage["cost"] == 0
    assert transcriber.usage["unknown_cost_calls"] == 1


def test_litellm_map_prices_audio_tokens_for_the_recommended_model():
    usage = litellm.Usage(
        prompt_tokens=1500,
        completion_tokens=170,
        prompt_tokens_details={"audio_tokens": 1500},
    )
    prompt, completion = litellm.cost_per_token(model="gemini-3.5-flash", usage_object=usage)
    assert prompt > 0
    assert completion > 0


def test_a_failed_chunk_fails_the_recording_and_cancels_the_rest(
    monkeypatch, tmp_path, chunked, priced
):
    chunked(3000)
    sent = []

    def complete(_self, params):
        sent.append(params)
        message = "Connection error. internal-host.example secret-token"
        raise RuntimeError(message)

    monkeypatch.setattr(LLMClient, "_complete", complete)
    transcriber = Transcriber(LLM, max_concurrent=1)
    with pytest.raises(TranscriptionError) as caught:
        transcriber.transcribe(tmp_path / "a.mp3", tmp_path)
    assert "secret-token" not in str(caught.value)
    assert "internal-host" not in str(caught.value)
    # Of 10 chunks only the failed one and at most one already picked up
    # by the worker are sent; the queued rest are cancelled.
    assert len(sent) <= 2
    assert transcriber.usage["calls"] == len(sent)
    assert transcriber.usage["unknown_cost_calls"] == len(sent)


def test_chunk_logs_name_the_recording(monkeypatch, tmp_path, chunked, priced):
    chunked(650)
    seen = []

    def complete(_self, _params):
        seen.append(current_document())
        return _response()

    monkeypatch.setattr(LLMClient, "_complete", complete)
    with document_scope("a.mp3"):
        Transcriber(LLM, max_concurrent=2).transcribe(tmp_path / "a.mp3", tmp_path)
    assert seen == ["a.mp3", "a.mp3"]


def test_rate_limits_are_retried(monkeypatch, tmp_path, chunked, priced):
    chunked(60)
    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: 0)
    attempts = []

    class RateLimitError(RuntimeError):
        status_code = 429

    def limited_once(_self, _params):
        attempts.append(1)
        if len(attempts) == 1:
            message = "rate limited"
            raise RateLimitError(message)
        return _response("ok")

    monkeypatch.setattr(LLMClient, "_complete", limited_once)
    transcriber = Transcriber(LLM, max_concurrent=1)
    text = transcriber.transcribe(tmp_path / "a.mp3", tmp_path)
    assert text == "[00:00:00] ok"
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "response",
    [
        _response(finish="length"),
        SimpleNamespace(choices=[], usage=None),
    ],
)
def test_truncated_or_empty_responses_fail(monkeypatch, tmp_path, chunked, priced, response):
    chunked(60)
    monkeypatch.setattr(LLMClient, "_complete", lambda *_: response)
    with pytest.raises(TranscriptionError, match=r"truncated|Empty response"):
        Transcriber(LLM, max_concurrent=1).transcribe(tmp_path / "a.mp3", tmp_path)


def test_silent_chunks_are_left_out(monkeypatch, tmp_path, chunked, priced):
    chunked(650)
    monkeypatch.setattr(
        LLMClient,
        "_complete",
        lambda _self, params: _response("" if _chunk_name(params) == "chunk-0" else "words"),
    )
    text = Transcriber(LLM, max_concurrent=2).transcribe(tmp_path / "a.mp3", tmp_path)
    assert text == "[00:05:00] words"


def test_recordings_without_audio_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(transcription, "_envelope", lambda _src: [])
    with pytest.raises(TranscriptionError, match="no decodable audio"):
        Transcriber(LLM, max_concurrent=1).transcribe(tmp_path / "a.mp3", tmp_path)


def test_concurrency_is_bounded_across_recordings(monkeypatch, tmp_path, chunked, priced):
    chunked(3000)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def complete(_self, _params):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return _response()

    monkeypatch.setattr(LLMClient, "_complete", complete)
    transcriber = Transcriber(LLM, max_concurrent=2)
    threads = [
        threading.Thread(
            target=transcriber.transcribe,
            args=(tmp_path / f"{name}.mp3", tmp_path / name),
        )
        for name in "abc"
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["peak"] == 2
    assert transcriber.usage["calls"] == 30


def test_build_transcriber_resolves_the_tier():
    assert build_transcriber({"model": "primary"}) is None
    transcriber = build_transcriber(
        {
            "transcription_model": "transcribe",
            "models": {"transcribe": "gemini-3.5-flash"},
            "llm_config": {"base_url": BASE_URL, "api_key": "k"},
            "max_concurrent": 3,
            "timeout_seconds": 90,
        }
    )
    assert transcriber.identity == "transcript:gemini-3.5-flash:v1"
    assert transcriber._workers == 3
    assert transcriber._llm["base_url"] == BASE_URL
    assert transcriber._timeout == 90


# -------------------------------------------------------------------- cache


class FakeTranscriber:
    def __init__(self, model="m1", text="spoken words"):
        self.model = model
        self.identity = f"transcript:{model}:v1"
        self.text = text
        self.calls = 0
        self.usage = {}

    def transcribe(self, source, workdir):
        self.calls += 1
        assert workdir == source.parent / ".text"
        return self.text


@pytest.fixture
def recording(tmp_path) -> Path:
    path = tmp_path / "meeting.mp3"
    path.write_bytes(b"ID3 fake audio")
    return path


def test_transcripts_are_cached_by_source_and_model(recording):
    first = FakeTranscriber()
    assert transcript(recording, first) == "spoken words"
    assert transcript(recording, first) == "spoken words"
    assert first.calls == 1
    assert read_text_cache(recording, return_meta=True)[1]["method"] == ("transcript:m1:v1")
    changed = FakeTranscriber(model="m2", text="other words")
    assert transcript(recording, changed) == "other words"
    assert changed.calls == 1


def test_changed_audio_is_transcribed_again(recording):
    transcriber = FakeTranscriber()
    transcript(recording, transcriber)
    recording.write_bytes(b"ID3 different audio bytes")
    transcript(recording, transcriber)
    assert transcriber.calls == 2


def test_readers_without_a_transcriber_use_the_cached_transcript(recording):
    transcript(recording, FakeTranscriber())
    assert transcript(recording, None) == "spoken words"
    assert extract_text_from_document(recording) == "spoken words"


def test_uncached_audio_without_a_transcriber_names_the_config_key(recording):
    with pytest.raises(
        TranscriptionError,
        match=r"meeting\.mp3 is audio.*transcription_model: <tier>",
    ):
        transcript(recording, None)


def test_document_dispatch_returns_transcript_and_scope(recording):
    text, meta = extract_text_from_document(
        recording, return_meta=True, transcriber=FakeTranscriber()
    )
    assert text == "spoken words"
    assert meta == {"used_ocr": False, "page_ranges": None, "page_count": None}


def test_audio_is_a_supported_and_downloadable_document(tmp_path):
    for ext in AUDIO_EXTENSIONS:
        assert is_supported_document(tmp_path / f"a{ext.upper()}")
        assert ext in DOCUMENT_EXTENSIONS
    assert MIME_TO_EXTENSION["audio/mpeg"] == ".mp3"
    infer = DiscoveryEngine._infer_extension_from_url_or_mime
    assert infer("https://x.example/files/m4", "audio/mpeg") == ".mp3"
    assert infer("https://x.example/a.MP3", "application/octet-stream") == (".mp3")
    assert infer("https://x.example/a", "audio/x-m4a; q=1") == ".m4a"


# ------------------------------------------------------------------- config


def _config(tmp_path, extraction, models=None) -> Path:
    run = {
        "domain": "meetings",
        "schema": "schema.json",
        "models": models or {"primary": "m-p", "transcribe": "m-t"},
        "extraction": {"model": "primary", **extraction},
    }
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(run), encoding="utf-8")
    return path


def test_transcription_model_flows_to_extraction(tmp_path):
    config = load_runtime_config_file(_config(tmp_path, {"transcription_model": "transcribe"}))
    resolved = resolve_command_config(command="extract", cli_values={}, config_data=config)
    assert resolved["transcription_model"] == "transcribe"


@pytest.mark.parametrize(
    ("extraction", "message"),
    [
        (
            {"transcription_model": "transcibe"},
            (
                "extraction.transcription_model: 'transcibe' is not a "
                "models: alias — did you mean 'transcribe'"
            ),
        ),
        (
            {"transcription_model": "gemini-3.5-flash"},
            "'gemini-3.5-flash' is not a models: alias",
        ),
        (
            {"transcription_model": ["transcribe"]},
            "extraction.transcription_model must be a string",
        ),
    ],
)
def test_transcription_model_is_validated(tmp_path, extraction, message):
    with pytest.raises(RuntimeConfigError, match=message):
        load_runtime_config_file(_config(tmp_path, extraction))


def test_misspelled_transcription_key_suggests_the_key(tmp_path):
    config = load_runtime_config_file(_config(tmp_path, {"transcription_modle": "transcribe"}))
    with pytest.raises(RuntimeConfigError, match="did you mean 'transcription_model'"):
        resolve_command_config(command="extract", cli_values={}, config_data=config)


# ----------------------------------------------------------------- pipeline


def _fingerprints(tmp_path, documents, transcriber):
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    llm = {"model": "m", "provider": "openai"}
    return pipeline._extraction_fingerprints(
        {}, llm, None, schema, documents, {}, {}, None, {}, transcriber
    )


def test_audio_fingerprint_tracks_transcriber_not_cache(tmp_path, recording):
    note = tmp_path / "note.txt"
    note.write_text("text", encoding="utf-8")
    docs = [recording, note]
    transcriber = FakeTranscriber()
    before = _fingerprints(tmp_path, docs, transcriber)
    transcript(recording, transcriber)
    after = _fingerprints(tmp_path, docs, transcriber)
    assert after[recording] == before[recording]
    other = _fingerprints(tmp_path, docs, FakeTranscriber(model="m2"))
    assert other[recording] != before[recording]
    plain = _fingerprints(tmp_path, docs, None)
    assert other[note] == before[note] == plain[note]


def test_extraction_reads_transcripts_and_bills_transcription(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "meeting.mp3").write_bytes(b"ID3 fake audio")
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {"main_data_array": "items"},
                    "identity": {
                        "deduplication": {
                            "key_fields": ["name"],
                            "ignore_fields": [],
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    seen = []

    class Extractor:
        def __init__(self, **_kwargs):
            pass

        def extract(self, text, _schema):
            seen.append(text)
            return SimpleNamespace(data={"items": [{"name": "a"}]}, cost=0.5)

    fake = FakeTranscriber(model="gemini-3.5-flash", text="[00:00:00] I support the project.")
    fake.usage = {
        "calls": 3,
        "cost": 0.25,
        "input_tokens": 22500,
        "output_tokens": 900,
        "unknown_cost_calls": 0,
    }
    monkeypatch.setattr("psweep.extraction.DocumentExtractor", Extractor)
    monkeypatch.setattr(pipeline.transcription, "build_transcriber", lambda _inputs: fake)
    inputs = {
        "path": str(docs),
        "schema": str(schema),
        "output": str(tmp_path / "out"),
        "model": "primary",
        "models": {"primary": "gpt-x", "transcribe": "gemini-3.5-flash"},
        "llm_config": {"base_url": BASE_URL, "api_key": "k"},
        "transcription_model": "transcribe",
        "skip_existing": True,
    }
    result = pipeline._execute_extraction(pipeline._prepare_extraction(inputs))
    assert result.successful == 1
    assert seen == ["[00:00:00] I support the project."]
    manifest = json.loads(next((tmp_path / "out" / "run_manifests").glob("*.json")).read_text())
    costs = manifest["costs"]
    assert costs["total_cost_usd"] == pytest.approx(0.75)
    assert costs["llm_calls"] == 4
    assert costs["total_input_tokens"] == 22500
    assert costs["models"] == ["gemini-3.5-flash", "gpt-x"]
    assert "model" not in costs

    rerun = pipeline._prepare_extraction(inputs)
    assert rerun["pending"] == []
    assert fake.calls == 1


# -------------------------------------------------------------- integration


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_ffmpeg_cuts_at_silence(monkeypatch, tmp_path, priced):
    source = tmp_path / "tone.wav"
    tone = r"0.4*sin(2*PI*440*t)*not(between(t\,309\,311))"
    subprocess.run(  # noqa: S603
        [
            shutil.which("ffmpeg"),
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={tone}:s=8000:d=400",
            str(source),
        ],
        check=True,
    )
    starts = chunk_starts(transcription._envelope(source))
    assert len(starts) == 2
    assert 309 <= starts[1] <= 311
    sizes = []

    def complete(_self, params):
        data = params["messages"][0]["content"][1]["input_audio"]["data"]
        sizes.append(len(base64.b64decode(data)))
        return _response("tone")

    monkeypatch.setattr(LLMClient, "_complete", complete)
    text = transcript(source, Transcriber(LLM, max_concurrent=2))
    assert text == f"[00:00:00] tone\n\n{timestamp(starts[1])} tone"
    # 32 kbps mono: ~4 kB per second of audio.
    assert max(sizes) == pytest.approx(309 * 4000, rel=0.1)
    assert not list((tmp_path / ".text").glob(".chunks-*"))
    assert document_utils.read_text_cache(source) == text


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (401, False)])
def test_transcription_error_records_keep_provider_retryability(
    monkeypatch, tmp_path, chunked, priced, status, retryable
):
    from psweep.utils.error_taxonomy import build_error_record

    chunked(60)
    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: 0)

    class ProviderError(RuntimeError):
        status_code = status

    def fail(_self, _params):
        raise ProviderError("service unavailable secret-token")

    monkeypatch.setattr(LLMClient, "_complete", fail)
    with pytest.raises(TranscriptionError) as caught:
        Transcriber(LLM, max_concurrent=1).transcribe(tmp_path / "a.mp3", tmp_path)
    assert build_error_record(caught.value, stage="extract")["retryable"] is retryable
