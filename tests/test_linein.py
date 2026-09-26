"""Line-in capture, driven with synthetic 2-channel audio — no hardware needed."""
from __future__ import annotations

import numpy as np
import pytest

from meeting_capture import linein
from meeting_capture.recorder import CHUNK_DURATION, ROLE_MIC, ROLE_SYSTEM, SAMPLE_RATE

SR = SAMPLE_RATE
BLK = int(SR * CHUNK_DURATION)  # samples per channel per block


def _tone(seconds: float, amp: int = 8000) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * 440 * t)).astype("<i2")


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype="<i2")


def _stream(me: np.ndarray, them: np.ndarray) -> list[bytes]:
    """Interleave two equal-length channels into 2-channel int16 blocks."""
    n = max(me.size, them.size)
    me = np.pad(me, (0, n - me.size))
    them = np.pad(them, (0, n - them.size))
    inter = np.empty(n * 2, dtype="<i2")
    inter[0::2], inter[1::2] = me, them
    step = BLK * 2
    return [inter[i:i + step].tobytes() for i in range(0, inter.size, step)]


def _pump(blocks, tmp_path, should_record=lambda: True, me_ch=0, them_ch=1):
    return list(linein.pump_blocks(iter(blocks), tmp_path, should_record, 2, me_ch, them_ch))


def test_speech_on_me_channel_only_yields_one_me_chunk(tmp_path):
    me = np.concatenate([_tone(10), _silence(4)])
    chunks = _pump(_stream(me, _silence(14)), tmp_path)
    assert [c.role for c in chunks] == [ROLE_MIC]
    assert 9 < chunks[0].duration_seconds <= 10.5
    assert chunks[0].path.exists()


def test_both_sides_are_split_and_labelled(tmp_path):
    me = np.concatenate([_tone(10), _silence(18)])
    them = np.concatenate([_silence(14), _tone(10), _silence(4)])
    chunks = _pump(_stream(me, them), tmp_path)
    assert sorted(c.role for c in chunks) == sorted([ROLE_MIC, ROLE_SYSTEM])
    # each chunk came from its own channel: the me chunk starts before the them chunk
    by_role = {c.role: c for c in chunks}
    assert by_role[ROLE_MIC].started_at <= by_role[ROLE_SYSTEM].started_at


def test_channel_map_decides_the_role(tmp_path):
    """Speech on interface input 1 (ch0) is 'me' by default, 'them' if remapped."""
    speech_on_ch0 = _stream(np.concatenate([_tone(10), _silence(4)]), _silence(14))
    assert [c.role for c in _pump(speech_on_ch0, tmp_path)] == [ROLE_MIC]
    assert [c.role for c in _pump(speech_on_ch0, tmp_path, me_ch=1, them_ch=0)] == [ROLE_SYSTEM]


def test_stopping_mid_speech_flushes_the_in_flight_chunk(tmp_path):
    blocks = _stream(_tone(20), _silence(20))
    fed = {"n": 0}
    stop_after = int(5 / CHUNK_DURATION)  # 5s of speech — past FLUSH_MIN_SECONDS

    def should_record():
        fed["n"] += 1
        return fed["n"] <= stop_after

    chunks = _pump(blocks, tmp_path, should_record=should_record)
    assert [c.role for c in chunks] == [ROLE_MIC]
    assert 4 < chunks[0].duration_seconds <= 5.5


def test_consumer_stopping_early_does_not_raise_and_closes_the_source(tmp_path):
    closed = {"yes": False}

    def source():
        try:
            yield from _stream(np.concatenate([_tone(10), _silence(4)] * 3), _silence(42))
        finally:
            closed["yes"] = True

    gen = linein.pump_blocks(source(), tmp_path, lambda: True, 2, 0, 1)
    next(gen)          # take the first chunk, then abandon the iterator
    gen.close()        # must not raise "generator ignored GeneratorExit"
    assert closed["yes"], "audio source was not closed on early exit"


def test_torn_trailing_frame_is_dropped_not_fatal(tmp_path):
    blocks = _stream(np.concatenate([_tone(10), _silence(4)]), _silence(14))
    blocks[-1] = blocks[-1] + b"\x01\x00"  # one extra sample → an odd, torn frame
    assert [c.role for c in _pump(blocks, tmp_path)] == [ROLE_MIC]


def test_silence_on_both_channels_costs_nothing(tmp_path):
    assert _pump(_stream(_silence(30), _silence(30)), tmp_path) == []


def test_mode_and_channel_env(monkeypatch):
    monkeypatch.delenv(linein.SOURCE_ENV, raising=False)
    assert not linein.linein_mode_enabled()
    monkeypatch.setenv(linein.SOURCE_ENV, "LineIn")
    assert linein.linein_mode_enabled()
    monkeypatch.delenv(linein.ME_CHANNEL_ENV, raising=False)
    monkeypatch.delenv(linein.THEM_CHANNEL_ENV, raising=False)
    assert (linein.me_channel(), linein.them_channel()) == (0, 1)
    monkeypatch.setenv(linein.ME_CHANNEL_ENV, "1")
    monkeypatch.setenv(linein.THEM_CHANNEL_ENV, "not-a-number")
    assert (linein.me_channel(), linein.them_channel()) == (1, 1)


class _FakeSD:
    def __init__(self, devices):
        self._devices = devices

    def query_devices(self, *args, **kwargs):
        return self._devices


@pytest.fixture
def fake_sd(monkeypatch):
    devices = [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1},
        {"name": "MacBook Pro Speakers", "max_input_channels": 0},
        {"name": "UMC202HD 192k", "max_input_channels": 2},
    ]
    monkeypatch.setattr(linein, "_import_sounddevice", lambda: _FakeSD(devices))
    return devices


def test_resolve_device_by_name_substring(fake_sd):
    assert linein.resolve_device("umc202") == 2
    assert linein.resolve_device("3") == 3
    assert linein.resolve_device("") is None


def test_resolve_device_explains_misses_and_ambiguity(fake_sd):
    with pytest.raises(RuntimeError, match="no input device matching"):
        linein.resolve_device("focusrite")
    # "m" is in both input devices' names ("MacBook Pro Microphone", "UMC202HD");
    # the output-only "Speakers" device is filtered out and can't contribute.
    with pytest.raises(RuntimeError, match="matches several"):
        linein.resolve_device("m")


def test_list_input_devices_skips_output_only(fake_sd):
    assert [d["name"] for d in linein.list_input_devices()] == ["MacBook Pro Microphone", "UMC202HD 192k"]
