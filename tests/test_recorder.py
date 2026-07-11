import numpy as np

from meeting_capture import recorder


def test_rms_int16_silence():
    silent = np.zeros(1000, dtype=np.int16)
    assert recorder._rms_int16(silent) == 0.0


def test_rms_int16_signal():
    val = int(0.2 * 32768)
    sig = np.full(1000, val, dtype=np.int16)
    assert abs(recorder._rms_int16(sig) - 0.2) < 1e-3


def test_trim_trailing_silence_keeps_signal():
    sample_rate = recorder.SAMPLE_RATE
    block = int(sample_rate * recorder.CHUNK_DURATION)
    speech_val = int(0.3 * 32768)
    speech = np.full(block * 4, speech_val, dtype=np.int16)
    silence = np.zeros(block * 6, dtype=np.int16)
    audio = np.concatenate([speech, silence])
    trimmed = recorder._trim_trailing_silence(audio, sample_rate)
    assert len(trimmed) <= len(speech) + block
    assert len(trimmed) >= len(speech) - block


def test_trim_trailing_silence_all_silent():
    sample_rate = recorder.SAMPLE_RATE
    block = int(sample_rate * recorder.CHUNK_DURATION)
    audio = np.zeros(block * 5, dtype=np.int16)
    trimmed = recorder._trim_trailing_silence(audio, sample_rate)
    assert len(trimmed) <= block


def test_find_audiotee_via_env(tmp_path, monkeypatch):
    fake = tmp_path / "audiotee"
    fake.write_text("")
    monkeypatch.setenv(recorder.AUDIOTEE_ENV_VAR, str(fake))
    assert recorder.find_audiotee() == fake


def test_find_audiotee_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv(recorder.AUDIOTEE_ENV_VAR, raising=False)
    monkeypatch.setattr(recorder.shutil, "which", lambda _name: None)
    monkeypatch.setattr(recorder.Path, "is_file", lambda self: False)
    assert recorder.find_audiotee() is None


def test_find_sysaudio_via_env(tmp_path, monkeypatch):
    fake = tmp_path / "sysaudio"
    fake.write_text("")
    monkeypatch.setenv(recorder.SYSAUDIO_ENV_VAR, str(fake))
    assert recorder.find_sysaudio() == fake


def test_find_sysaudio_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv(recorder.SYSAUDIO_ENV_VAR, raising=False)
    monkeypatch.setattr(recorder.shutil, "which", lambda _name: None)
    monkeypatch.setattr(recorder.Path, "is_file", lambda self: False)
    assert recorder.find_sysaudio() is None


def test_find_capture_binary_prefers_sysaudio(tmp_path, monkeypatch):
    fake_sysaudio = tmp_path / "sysaudio"
    fake_audiotee = tmp_path / "audiotee"
    fake_sysaudio.write_text("")
    fake_audiotee.write_text("")
    monkeypatch.setenv(recorder.SYSAUDIO_ENV_VAR, str(fake_sysaudio))
    monkeypatch.setenv(recorder.AUDIOTEE_ENV_VAR, str(fake_audiotee))
    assert recorder.find_capture_binary() == fake_sysaudio


def test_find_capture_binary_falls_back_to_audiotee(tmp_path, monkeypatch):
    fake_audiotee = tmp_path / "audiotee"
    fake_audiotee.write_text("")
    monkeypatch.delenv(recorder.SYSAUDIO_ENV_VAR, raising=False)
    monkeypatch.setenv(recorder.AUDIOTEE_ENV_VAR, str(fake_audiotee))
    monkeypatch.setattr(recorder.shutil, "which", lambda name: None)
    # Also need to patch the bin/ search; easiest is monkeypatching find_sysaudio to None
    monkeypatch.setattr(recorder, "find_sysaudio", lambda: None)
    assert recorder.find_capture_binary() == fake_audiotee


def test_flush_min_seconds_constant_exists():
    assert recorder.FLUSH_MIN_SECONDS > 0
    assert recorder.FLUSH_MIN_SECONDS < recorder.MIN_CHUNK_SECONDS


# ---------------------------------------------------------------------------
# Mic (own-voice) capture gating


def test_mic_capture_enabled_when_supported(monkeypatch):
    monkeypatch.delenv(recorder.MIC_ENV_VAR, raising=False)
    monkeypatch.setattr(recorder, "mic_capture_supported", lambda: True)
    assert recorder.mic_capture_enabled() is True


def test_mic_capture_env_opt_out(monkeypatch):
    monkeypatch.setattr(recorder, "mic_capture_supported", lambda: True)
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv(recorder.MIC_ENV_VAR, off)
        assert recorder.mic_capture_enabled() is False


def test_mic_capture_needs_macos_15(monkeypatch):
    monkeypatch.delenv(recorder.MIC_ENV_VAR, raising=False)
    monkeypatch.setattr(recorder.platform, "mac_ver", lambda: ("14.7.1", ("", "", ""), ""))
    assert recorder.mic_capture_supported() is False
    assert recorder.mic_capture_enabled() is False
    monkeypatch.setattr(recorder.platform, "mac_ver", lambda: ("15.0", ("", "", ""), ""))
    assert recorder.mic_capture_supported() is True


# ---------------------------------------------------------------------------
# Frame parser (sysaudio --mic wire protocol)


def _frame(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + len(payload).to_bytes(4, "little") + payload


def test_frame_parser_whole_frames():
    p = recorder._FrameParser()
    frames = p.feed(
        _frame(recorder.FRAME_TAG_SYSTEM, b"aa") + _frame(recorder.FRAME_TAG_MIC, b"bbbb")
    )
    assert frames == [
        (recorder.FRAME_TAG_SYSTEM, b"aa"),
        (recorder.FRAME_TAG_MIC, b"bbbb"),
    ]


def test_frame_parser_handles_byte_dribble():
    p = recorder._FrameParser()
    data = _frame(recorder.FRAME_TAG_MIC, b"hello")
    frames = []
    for i in range(len(data)):
        frames.extend(p.feed(data[i : i + 1]))
    assert frames == [(recorder.FRAME_TAG_MIC, b"hello")]


def test_frame_parser_split_across_reads():
    p = recorder._FrameParser()
    data = _frame(recorder.FRAME_TAG_SYSTEM, b"x" * 100)
    assert p.feed(data[:7]) == []
    assert p.feed(data[7:]) == [(recorder.FRAME_TAG_SYSTEM, b"x" * 100)]


def test_frame_parser_raises_on_unknown_tag():
    import pytest

    p = recorder._FrameParser()
    with pytest.raises(ValueError):
        p.feed(_frame(0x51, b"zz"))


def test_frame_parser_raises_on_oversized_payload():
    import pytest

    p = recorder._FrameParser()
    bad = bytes([recorder.FRAME_TAG_SYSTEM]) + (recorder.MAX_FRAME_PAYLOAD + 1).to_bytes(4, "little")
    with pytest.raises(ValueError):
        p.feed(bad)


# ---------------------------------------------------------------------------
# Per-channel chunker


def _speech_block() -> bytes:
    val = int(0.2 * 32768)
    return np.full(recorder.SAMPLES_PER_BLOCK, val, dtype=np.int16).tobytes()


def _silent_block() -> bytes:
    return np.zeros(recorder.SAMPLES_PER_BLOCK, dtype=np.int16).tobytes()


def _feed_seconds(chunker, block: bytes, seconds: float):
    chunks = []
    n = int(seconds / recorder.CHUNK_DURATION)
    for _ in range(n):
        chunks.extend(chunker.feed_bytes(block, recorder.BLOCK_BYTES))
    return chunks


def test_chunker_emits_after_speech_then_gap(tmp_path):
    c = recorder._ChannelChunker(recorder.ROLE_MIC, tmp_path, recorder.SAMPLE_RATE)
    chunks = _feed_seconds(c, _speech_block(), recorder.MIN_CHUNK_SECONDS + 2)
    assert chunks == []  # still buffering — no gap yet
    chunks = _feed_seconds(c, _silent_block(), recorder.SILENCE_GAP_SECONDS + 0.5)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.role == "me"
    assert chunk.path.exists()
    assert chunk.path.name.endswith("-me.wav")
    assert chunk.duration_seconds >= recorder.MIN_CHUNK_SECONDS


def test_chunker_ignores_pure_silence(tmp_path):
    c = recorder._ChannelChunker(recorder.ROLE_SYSTEM, tmp_path, recorder.SAMPLE_RATE)
    chunks = _feed_seconds(c, _silent_block(), 20.0)
    assert chunks == []
    assert c.silent_audio_s >= 19.0  # stolen-capture detector is ticking


def test_chunker_silent_counter_resets_on_speech(tmp_path):
    c = recorder._ChannelChunker(recorder.ROLE_SYSTEM, tmp_path, recorder.SAMPLE_RATE)
    _feed_seconds(c, _silent_block(), 10.0)
    assert c.silent_audio_s >= 9.0
    _feed_seconds(c, _speech_block(), 1.0)
    assert c.silent_audio_s == 0.0


def test_chunker_flush_emits_short_tail(tmp_path):
    c = recorder._ChannelChunker(recorder.ROLE_SYSTEM, tmp_path, recorder.SAMPLE_RATE)
    _feed_seconds(c, _speech_block(), recorder.FLUSH_MIN_SECONDS + 1)
    chunk = c.flush()
    assert chunk is not None
    assert chunk.role == "them"
    assert chunk.path.name.endswith("-them.wav")


def test_chunker_flush_drops_too_short_tail(tmp_path):
    c = recorder._ChannelChunker(recorder.ROLE_SYSTEM, tmp_path, recorder.SAMPLE_RATE)
    _feed_seconds(c, _speech_block(), recorder.FLUSH_MIN_SECONDS / 2)
    assert c.flush() is None
    assert c.flush() is None  # idempotent — buffer was consumed


def test_chunker_partial_blocks_accumulate(tmp_path):
    c = recorder._ChannelChunker(recorder.ROLE_MIC, tmp_path, recorder.SAMPLE_RATE)
    block = _speech_block()
    # Feed one block in two unaligned halves — must not process until complete.
    assert c.feed_bytes(block[: len(block) // 3], recorder.BLOCK_BYTES) == []
    assert c.buffer == []
    c.feed_bytes(block[len(block) // 3 :], recorder.BLOCK_BYTES)
    assert len(c.buffer) == 1


# ---------------------------------------------------------------------------
# Disclaimed spawn (TCC responsibility handoff to sysaudio)


def test_disclaimed_proc_runs_and_pipes_stdout():
    p = recorder._DisclaimedProc(["/bin/echo", "hello-disclaimed"])
    out = p.stdout.read()
    assert out == b"hello-disclaimed\n"
    assert p.wait(timeout=5) == 0


def test_disclaimed_proc_terminate():
    import subprocess

    p = recorder._DisclaimedProc(["/bin/sleep", "30"])
    p.terminate()
    try:
        rc = p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        raise
    assert rc != 0  # killed by signal


def test_spawn_capture_falls_back_on_failure(monkeypatch):
    def boom(cmd):
        raise OSError("nope")

    monkeypatch.setattr(recorder, "_DisclaimedProc", boom)
    p = recorder._spawn_capture(["/bin/echo", "fallback"], disclaim=True)
    assert p.stdout.read() == b"fallback\n"
    p.wait(timeout=5)


def test_chunk_default_role_is_them():
    from pathlib import Path

    chunk = recorder.Chunk(path=Path("/tmp/x.wav"), started_at=0.0, duration_seconds=1.0)
    assert chunk.role == "them"
