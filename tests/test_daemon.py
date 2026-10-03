import pytest
from pathlib import Path

from meeting_capture import daemon, store
from meeting_capture.recorder import Chunk


def _body(meeting_id):
    row = store.get(meeting_id)
    return row["body"] if row else None


def _ready(engine="apple", choice="auto", ready=True):
    from meeting_capture.transcriber import Backend
    return Backend(engine, choice, "test", "en-US", ready)


def _wait(cond, timeout=5.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


def test_session_id_contains_timestamp():
    sid = daemon._session_id(1714003200.0)
    assert sid.startswith("meeting-") and "T" in sid and not sid.endswith(".md")
    assert daemon._started_iso(sid).startswith(sid[len("meeting-"):len("meeting-") + 10])


def test_append_creates_header_then_appends(tmp_path):
    chunk = Chunk(path=Path("/tmp/x.wav"), started_at=1714003200.0, duration_seconds=5.0)
    daemon._append("meeting-x", chunk, "first line")
    daemon._append("meeting-x", chunk, "second line")
    text = _body("meeting-x")
    assert text.count("# Meeting transcript") == 1
    assert "first line" in text
    assert "second line" in text
    assert not (tmp_path / "transcripts").exists(), "no transcript files"


def test_append_skips_empty():
    chunk = Chunk(path=Path("/tmp/x.wav"), started_at=1714003200.0, duration_seconds=5.0)
    daemon._append("meeting-y", chunk, "")
    assert _body("meeting-y") is None


def test_append_labels_roles():
    them = Chunk(path=Path("/tmp/a.wav"), started_at=1714003200.0, duration_seconds=5.0, role="them")
    me = Chunk(path=Path("/tmp/b.wav"), started_at=1714003210.0, duration_seconds=5.0, role="me")
    daemon._append("meeting-z", them, "how was the launch?")
    daemon._append("meeting-z", me, "shipped last night")
    text = _body("meeting-z")
    assert "**Them:** how was the launch?" in text
    assert "**Me:** shipped last night" in text


def test_locked_db_queues_the_line_and_flushes_it_later(tmp_path, monkeypatch):
    import sqlite3
    real = store._write
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "_write", locked)
    daemon._append_text("meeting-q", "them", "said while the db was locked", started_at=1714003200.0)
    assert store.PENDING_FILE.exists()
    monkeypatch.setattr(store, "_write", real)
    daemon._append_text("meeting-q", "me", "and then this", started_at=1714003201.0)
    text = _body("meeting-q")
    assert text.index("while the db was locked") < text.index("and then this")
    assert not store.PENDING_FILE.exists()


def test_backoff_escalates_on_fast_failures_and_resets():
    b = daemon.FailureBackoff(fast_fail_s=10.0, base_s=5.0, max_s=40.0)
    assert b.record(0.2, 0) == 5.0
    assert b.record(0.2, 0) == 10.0
    assert b.record(0.2, 0) == 20.0
    assert b.record(0.2, 0) == 40.0
    assert b.record(0.2, 0) == 40.0  # capped
    assert b.failures == 5
    assert b.record(0.2, 1) == 0.0   # a chunk resets the streak
    assert b.failures == 0
    assert b.delay == 0.0


def test_backoff_ignores_long_sessions_without_chunks():
    b = daemon.FailureBackoff(fast_fail_s=10.0)
    assert b.record(45.0, 0) == 0.0  # quiet-but-alive session is not a failure
    assert b.failures == 0


def test_permission_hint_names_binary(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(daemon, "find_sysaudio", lambda: Path("/x/bin/sysaudio"))
    hint = daemon._permission_hint()
    assert "/x/bin/sysaudio" in hint
    assert "Screen & System Audio Recording" in hint


def _wav(path, seconds=1.0, rate=16000):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def test_park_failed_keeps_audio(tmp_path, monkeypatch):
    failed = tmp_path / "failed"
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    src = tmp_path / "chunk-1714003200-them.wav"; _wav(src)
    chunk = Chunk(path=src, started_at=1714003200.0, duration_seconds=1.0, role="them")
    dest = daemon._park_failed(chunk, RuntimeError("no key"))
    assert dest == failed / "chunk-1714003200-them.wav"
    assert dest.exists() and not src.exists()


def test_retry_recovers_parked_chunks_into_sessions(tmp_path, monkeypatch):
    failed = tmp_path / "failed"; failed.mkdir()
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    _wav(failed / "chunk-1714003200-them.wav", 2.0)
    _wav(failed / "chunk-1714003203-me.wav", 2.0)
    _wav(failed / "chunk-1714010000-them.wav", 2.0)   # > SESSION_GAP later → new session
    (failed / "not-a-chunk.wav").write_bytes(b"junk")
    said = {"them": "how was the launch?", "me": "shipped last night"}
    monkeypatch.setattr(daemon, "transcribe", lambda path, role: said[role])
    assert daemon.retry_failed_chunks() == 3
    assert sorted(p.name for p in failed.iterdir()) == ["not-a-chunk.wav"]
    sessions = sorted(r["meeting_id"] for r in store.recent())
    assert len(sessions) == 2
    first = _body(sessions[0])
    assert "**Them:** how was the launch?" in first and "**Me:** shipped last night" in first


def test_retry_stops_at_first_failure_and_keeps_the_rest(tmp_path, monkeypatch):
    failed = tmp_path / "failed"; failed.mkdir()
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    _wav(failed / "chunk-1714003200-them.wav"); _wav(failed / "chunk-1714003205-me.wav")
    def boom(path, role): raise RuntimeError("still no key")
    monkeypatch.setattr(daemon, "transcribe", boom)
    assert daemon.retry_failed_chunks() == 0
    assert len(list(failed.glob("chunk-*.wav"))) == 2


def test_retry_noop_without_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", tmp_path / "missing")
    assert daemon.retry_failed_chunks() == 0


def test_backlog_retry_never_holds_up_recording_and_new_chunks_go_first(tmp_path, monkeypatch):
    """A long backlog retry (e.g. after a bad key was replaced) runs on the
    transcription worker, one parked chunk at a time; new chunks jump ahead."""
    import threading, time
    failed = tmp_path / "failed"; failed.mkdir()
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    monkeypatch.setattr(daemon, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(daemon, "WORKER_IDLE_POLL_S", 0.01)
    for i in range(3):
        _wav(failed / f"chunk-{1714003200 + i * 10}-them.wav")
    gate, order = threading.Event(), []

    def slow(path, role):
        order.append(path.name)
        if len(order) == 1:
            gate.wait(5)            # the first backlog chunk is slow
        return "text " + path.name

    monkeypatch.setattr(daemon, "transcribe", slow)
    monkeypatch.setattr(daemon, "resolve_backend", lambda: _ready())
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w.request_retry()
    w.start()
    _wait(lambda: order)
    src = tmp_path / "chunk-1714009999-me.wav"; _wav(src)
    t0 = time.time()
    assert w.submit(Chunk(path=src, started_at=1714009999.0, duration_seconds=1.0, role="me"), "meeting-new")
    assert time.time() - t0 < 0.5             # capture side never waits
    gate.set()
    _wait(lambda: len(order) == 4)
    w.stop()
    assert order[1] == "chunk-1714009999-me.wav"   # the new chunk went before the rest of the backlog
    assert not list(failed.glob("chunk-*.wav"))


# ---- start a new meeting on request ----------------------------------------

@pytest.fixture
def new_meeting_file(tmp_path, monkeypatch):
    from meeting_capture import meetings
    f = tmp_path / "new-meeting"
    monkeypatch.setattr(meetings, "NEW_MEETING_FILE", f)
    return f


def test_chunks_stay_in_the_meeting_without_a_request(new_meeting_file):
    s1 = daemon._next_session(None, 1000.0, 0.0)
    assert daemon._next_session(s1, 1060.0, 1050.0) == s1          # 10 s gap: same meeting
    assert daemon._next_session(s1, 1050.0 + 16 * 60, 1050.0) != s1  # 16 min gap: new one


def test_new_meeting_request_splits_at_the_click(new_meeting_file):
    from meeting_capture import meetings
    s1 = daemon._next_session(None, 1000.0, 0.0)
    meetings.request_new_meeting(at=1100.0)
    # A chunk that started before the click still belongs to the old meeting…
    assert daemon._next_session(s1, 1090.0, 1085.0) == s1
    assert new_meeting_file.exists()
    # …the first one starting after it opens the new meeting, once.
    s2 = daemon._next_session(s1, 1101.0, 1095.0)
    assert s2 != s1 and not new_meeting_file.exists()
    assert daemon._next_session(s2, 1120.0, 1110.0) == s2


def test_back_to_back_meetings_in_the_same_second_get_distinct_ids(new_meeting_file):
    from meeting_capture import meetings
    s1 = daemon._next_session(None, 1000.0, 0.0)
    meetings.request_new_meeting(at=1000.0)
    assert daemon._next_session(s1, 1000.2, 1000.1) != s1


def test_resume_and_new_commands_request_a_new_meeting(new_meeting_file, tmp_path, monkeypatch, capsys):
    from meeting_capture import cli
    monkeypatch.setattr(cli, "PAUSE_FILE", tmp_path / "paused")
    (tmp_path / "paused").touch()
    assert cli.main(["resume"]) == 0
    assert new_meeting_file.exists() and "new transcript" in capsys.readouterr().out
    new_meeting_file.unlink()
    assert cli.main(["resume"]) == 0 and not new_meeting_file.exists()   # wasn't paused: no-op
    assert cli.main(["new"]) == 0 and new_meeting_file.exists()


# ---- capture → queue → one transcription worker ------------------------------

# pipeline-monitor's status._CHUNK_RE, verbatim: the menu bar's ● REC depends on it.
PIPELINE_MONITOR_CHUNK_RE = r"\bINFO chunk \d+(?:\.\d+)?s (?:\[\w+\] )?-> (\S+?) \(\d+ chars\)"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    audio.mkdir()
    failed = audio / "failed"
    monkeypatch.setattr(daemon, "AUDIO_DIR", audio)
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    monkeypatch.setattr(daemon, "WORKER_IDLE_POLL_S", 0.01)
    return audio, failed


def _chunk(audio, ts, role="them", seconds=1.0):
    p = audio / f"chunk-{ts}-{role}.wav"
    _wav(p, seconds)
    return Chunk(path=p, started_at=float(ts), duration_seconds=seconds, role=role)


def _lines(caplog):
    return [f"{r.levelname} {r.getMessage()}" for r in caplog.records]


def test_worker_appends_in_capture_order_and_keeps_the_log_contract(dirs, monkeypatch, caplog):
    import random, re, time
    audio, _ = dirs

    def fake(path, role):
        time.sleep(random.random() * 0.02)
        return f"said in {path.name}"

    monkeypatch.setattr(daemon, "transcribe", fake)
    monkeypatch.setattr(daemon, "last_backend", lambda: "apple")
    chunks = [_chunk(audio, 1714003200 + i * 10, "them" if i % 2 else "me") for i in range(6)]
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w.start()
    with caplog.at_level("INFO", logger="meeting-capture"):
        w.begin_session()
        for c in chunks[:4]:
            w.submit(c, "meeting-a")
        for c in chunks[4:]:
            w.submit(c, "meeting-b")
        w.end_session()
        _wait(lambda: any("session ended" in l for l in _lines(caplog)))
    w.stop()
    a, b = _body("meeting-a"), _body("meeting-b")
    positions = [a.index(c.path.name) for c in chunks[:4]]
    assert positions == sorted(positions)
    assert all(c.path.name in b for c in chunks[4:])
    lines = _lines(caplog)
    chunk_lines = [l for l in lines if re.search(PIPELINE_MONITOR_CHUNK_RE, l)]
    assert len(chunk_lines) == 6
    assert re.search(PIPELINE_MONITOR_CHUNK_RE, chunk_lines[0]).group(1) == "meeting-a"
    assert chunk_lines[0].startswith("INFO chunk 1.0s [me] -> meeting-a (")
    assert chunk_lines[0].endswith(" chars) via apple")
    # "session ended" comes after the session's last chunk line (REC state).
    assert lines.index(next(l for l in lines if "mic inactive — session ended" in l)) > lines.index(chunk_lines[-1])
    assert not list(audio.glob("chunk-*.wav"))


def test_session_end_is_not_logged_when_a_new_session_already_started(dirs, monkeypatch, caplog):
    import threading
    audio, _ = dirs
    gate = threading.Event()
    monkeypatch.setattr(daemon, "transcribe", lambda path, role: gate.wait(5) and "x")
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w.start()
    with caplog.at_level("INFO", logger="meeting-capture"):
        w.begin_session()
        w.submit(_chunk(audio, 1714003200), "meeting-a")
        w.end_session()
        w.begin_session()                    # the next call started while the worker was busy
        w.submit(_chunk(audio, 1714003260), "meeting-a")
        gate.set()
        _wait(lambda: w.idle() and len([l for l in _lines(caplog) if "INFO chunk" in l]) == 2)
    w.stop()
    assert not any("session ended" in l for l in _lines(caplog))


def test_capture_never_blocks_and_a_full_queue_parks_the_chunk(dirs, monkeypatch):
    import threading, time
    audio, failed = dirs
    gate = threading.Event()
    monkeypatch.setattr(daemon, "transcribe", lambda path, role: gate.wait(5) and "x")
    w = daemon.TranscriptionWorker(maxsize=2)
    w._next_check = float("inf")
    w.start()
    chunks = [_chunk(audio, 1714003200 + i * 10) for i in range(5)]
    t0 = time.time()
    results = [w.submit(c, "meeting-a") for c in chunks]
    assert time.time() - t0 < 0.5            # transcription is stuck; capture is not
    assert results[-1] is False
    parked = sorted(failed.glob("chunk-*.wav"))
    assert parked and daemon._read_meta(parked[0])["meeting_id"] == "meeting-a"
    gate.set()
    # The overflow is retried once the worker is idle, into the right meeting.
    _wait(lambda: _body("meeting-a") and _body("meeting-a").count("**Them:** x") == 5)
    w.stop()
    assert not list(failed.glob("chunk-*.wav"))


def test_a_bad_file_is_quarantined_after_three_attempts_and_never_blocks_the_rest(dirs, monkeypatch):
    from meeting_capture.transcriber import AppleChunkFailed
    _, failed = dirs
    failed.mkdir()
    bad = failed / "chunk-1714003200-them.wav"
    _wav(bad)

    def fake(path, role):
        if path.name == bad.name:
            raise AppleChunkFailed("couldn't read it")
        return "fine"

    monkeypatch.setattr(daemon, "transcribe", fake)
    for attempt in (1, 2):
        _wav(failed / f"chunk-{1714003300 + attempt}-me.wav")
        assert daemon.retry_failed_chunks() == 1          # the good one behind it still goes through
        assert daemon._read_meta(bad)["attempts"] == attempt
    assert daemon.retry_failed_chunks() == 0
    quarantined = failed / "quarantine" / bad.name
    assert quarantined.exists() and not bad.exists()
    assert daemon._read_meta(quarantined)["attempts"] == 3
    assert daemon.parked_chunks() == []
    assert daemon.parked_counts() == {"parked": 0, "quarantined": 1}


def test_an_unclassified_error_on_one_file_cannot_hold_up_the_rest(dirs, monkeypatch):
    """E.g. Gemini rejects one corrupt file with a 400: the next chunk is tried;
    it works, so the first failure counts toward that file's quarantine."""
    _, failed = dirs
    failed.mkdir()
    bad = failed / "chunk-1714003200-them.wav"
    _wav(bad)

    def fake(path, role):
        if path.name == bad.name:
            raise ValueError("400 INVALID_ARGUMENT: audio could not be decoded")
        return "fine"

    monkeypatch.setattr(daemon, "transcribe", fake)
    for attempt in (1, 2, 3):
        _wav(failed / f"chunk-{1714003300 + attempt}-me.wav")
        assert daemon.retry_failed_chunks() == 1
    assert (failed / "quarantine" / bad.name).exists()


def test_a_systemic_error_stops_the_pass_after_one_extra_try(dirs, monkeypatch):
    _, failed = dirs
    failed.mkdir()
    for i in range(4):
        _wav(failed / f"chunk-{1714003200 + i}-them.wav")
    calls = []

    def offline(path, role):
        calls.append(path.name)
        raise ConnectionError("network is unreachable")

    monkeypatch.setattr(daemon, "transcribe", offline)
    assert daemon.retry_failed_chunks() == 0
    assert len(calls) == 2                                   # not one request per parked chunk
    assert not any(daemon._read_meta(p).get("attempts") for p in failed.glob("chunk-*.wav"))
    assert len(list(failed.glob("chunk-*.wav"))) == 4


def test_a_malformed_wav_is_still_retried_and_quarantined(dirs, monkeypatch, fake_helper):
    """wave raises a bare RuntimeError on some malformed RIFF headers (found
    against the real engine); it must neither crash the retry pass nor be
    skipped for ever."""
    _, failed = dirs
    failed.mkdir()
    bad = failed / "chunk-1714003200-them.wav"
    bad.write_bytes(b"RIFF\x24\x00\x00\x00WAVEgarbage-not-audio")
    fake_helper.configure(transcribe_rc={bad.name: 70})
    assert [c.path.name for c in daemon.parked_chunks()] == [bad.name]
    for _ in range(3):
        daemon.retry_failed_chunks()
    assert (failed / "quarantine" / bad.name).exists()


def test_a_new_chunk_that_fails_on_its_own_is_counted_not_blocking(dirs, monkeypatch):
    from meeting_capture.transcriber import AppleError
    audio, failed = dirs
    calls = []

    def fake(path, role):
        calls.append(path.name)
        if len(calls) == 1:
            raise AppleError("helper exit 1")
        return "ok"

    monkeypatch.setattr(daemon, "transcribe", fake)
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w.start()
    first, second = _chunk(audio, 1714003200), _chunk(audio, 1714003210)
    w.submit(first, "meeting-a")
    w.submit(second, "meeting-a")
    _wait(lambda: len(calls) >= 2)
    w.stop()
    assert w.blocked is None
    assert daemon._read_meta(failed / first.path.name)["attempts"] == 1
    assert "**Them:** ok" in _body("meeting-a")


def test_unavailable_engine_parks_audio_and_waits_until_it_is_back(dirs, monkeypatch):
    import time
    from meeting_capture.transcriber import AppleUnavailable
    audio, failed = dirs
    monkeypatch.setattr(daemon, "ENGINE_RECHECK_S", 0.05)
    up, calls = {"ok": False}, []

    def fake(path, role):
        calls.append(path.name)
        if not up["ok"]:
            raise AppleUnavailable("the on-device model for en-US isn't installed yet")
        return "back again"

    monkeypatch.setattr(daemon, "transcribe", fake)
    monkeypatch.setattr(daemon, "resolve_backend", lambda: _ready(ready=up["ok"]))
    monkeypatch.setattr(daemon.TranscriptionWorker, "_maybe_install_model", lambda self: None)
    w = daemon.TranscriptionWorker().start()
    w.submit(_chunk(audio, 1714003200), "meeting-a")
    w.submit(_chunk(audio, 1714003210), "meeting-a")
    _wait(lambda: len(list(failed.glob("chunk-*.wav"))) == 2)
    assert w.blocked and "isn't installed" in w.blocked
    w.request_retry()
    n = len(calls)
    time.sleep(0.3)
    assert len(calls) == n                   # nothing is retried while unavailable
    assert all(daemon._read_meta(p).get("attempts") == 0 for p in failed.glob("chunk-*.wav"))
    up["ok"] = True
    _wait(lambda: not list(failed.glob("chunk-*.wav")))
    w.stop()
    assert w.blocked is None
    assert _body("meeting-a").count("back again") == 2


def test_retry_puts_a_chunk_back_into_the_meeting_it_was_recorded_in(dirs, monkeypatch):
    audio, failed = dirs
    c = _chunk(audio, 1714003500)
    daemon._park_failed(c, RuntimeError("network down"), "meeting-2024-04-25T00-00-00")
    monkeypatch.setattr(daemon, "transcribe", lambda path, role: "recovered words")
    assert daemon.retry_failed_chunks() == 1
    assert "recovered words" in _body("meeting-2024-04-25T00-00-00")
    assert list(failed.iterdir()) == []      # the audio and its note are both gone


def test_stopping_parks_what_is_still_queued(dirs):
    audio, failed = dirs
    w = daemon.TranscriptionWorker()         # never started: everything stays queued
    c = _chunk(audio, 1714003600)
    w.submit(c, "meeting-a")
    w.stop(timeout=0.1)
    assert (failed / c.path.name).exists() and not c.path.exists()
    assert daemon._read_meta(failed / c.path.name)["meeting_id"] == "meeting-a"


def test_chunks_left_behind_by_a_killed_daemon_are_adopted(dirs):
    import os, time
    audio, failed = dirs
    old = _chunk(audio, 1714003700).path
    fresh = _chunk(audio, 1714003800).path
    os.utime(old, (time.time() - 3600,) * 2)
    assert daemon.adopt_orphans() == 1
    assert (failed / old.name).exists() and fresh.exists()
    assert daemon.adopt_orphans(exclude={fresh}, min_age_s=0) == 0     # queued right now: not an orphan


def test_live_mode_survives_the_upgrade_to_on_device_transcription(fake_helper, gemini_key, monkeypatch):
    """An existing live-mode user (key set, no MEETING_CAPTURE_STT) on a Mac
    whose English model is installed: auto resolves batch to on this Mac,
    but live mode — their explicit choice to stream to Gemini — keeps working."""
    from meeting_capture import transcriber
    monkeypatch.setattr(daemon, "_live_refusal_logged", None)
    assert transcriber.resolve_backend().engine == "apple"
    assert daemon.live_permitted() is True
    # …and it doesn't flip once the daemon downloads a missing model either.
    fake_helper.configure(installed=[])
    assert transcriber.resolve_backend().engine == "gemini" and daemon.live_permitted() is True
    fake_helper.configure(installed=["en-US"])
    assert transcriber.resolve_backend().engine == "apple" and daemon.live_permitted() is True


def test_live_mode_is_refused_only_for_on_device_only_or_without_a_key(fake_helper, gemini_key, monkeypatch,
                                                                    caplog):
    monkeypatch.setattr(daemon, "_live_refusal_logged", None)
    monkeypatch.setenv("MEETING_CAPTURE_STT", "apple")
    with caplog.at_level("WARNING", logger="meeting-capture"):
        assert daemon.live_permitted() is False
        assert daemon.live_permitted() is False             # logged once, not per session
    assert "never upload" in caplog.text and caplog.text.count("MODE: live requested") == 1
    monkeypatch.setenv("MEETING_CAPTURE_STT", "gemini")
    assert daemon.live_permitted() is True
    monkeypatch.delenv("GOOGLE_API_KEY")
    for stt in ("auto", "gemini"):                         # live could not connect: batch keeps the audio
        monkeypatch.setenv("MEETING_CAPTURE_STT", stt)
        with caplog.at_level("WARNING", logger="meeting-capture"):
            assert daemon.live_permitted() is False
    assert "no Google API key" in caplog.text


# ---- a systemic failure is not the files' fault -----------------------------------------

def _drain(w):
    """Run the worker's retry of parked audio to the end, synchronously."""
    w.request_retry()
    for _ in range(100):
        w._idle()
        if w._pass is None and not w._retry_wanted:
            return
    raise AssertionError("retry pass never ended")


SYSTEMIC = {
    "exit 1": dict(default_rc=1),
    "no transcript": dict(no_json=True),
    "hang": dict(sleep=3),
    "crash": dict(signal=9),
}


@pytest.mark.parametrize("failure", list(SYSTEMIC))
def test_a_systemic_on_device_failure_never_quarantines_audio(dirs, fake_helper, monkeypatch, failure):
    """Apple's speech service broken (after an OS update), wedged or crashing
    while its probe still says usable: every chunk fails. Across a session,
    several session ends and restarts nothing may be counted against the
    files — and once the service is back, everything is transcribed."""
    from meeting_capture import transcriber
    audio, failed = dirs
    monkeypatch.setenv("MEETING_CAPTURE_STT", "apple")
    monkeypatch.setattr(transcriber, "APPLE_MIN_TIMEOUT_S", 0.2)
    fake_helper.configure(**SYSTEMIC[failure])
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    for i in range(4):                                    # one session
        w._new_chunk(_chunk(audio, 1714003200 + i * 20), "meeting-a")
    for n in range(3):                                    # session ends, daemon restarts
        _drain(w)
        daemon.retry_failed_chunks()
        daemon.TranscriptionWorker()._new_chunk(_chunk(audio, 1714003300 + n * 20), "meeting-a")
    parked = sorted(failed.glob("chunk-*.wav"))
    assert len(parked) == 7
    assert not list((failed / "quarantine").glob("*.wav"))
    assert [daemon._read_meta(p)["attempts"] for p in parked] == [0] * 7
    assert w.blocked is None                              # the probe still says usable
    fake_helper.configure(default_rc=0, no_json=False, sleep=0, signal=None)
    _drain(w)
    assert not list(failed.glob("chunk-*.wav"))
    assert _body("meeting-a").count("hello from this mac") == 7


def test_a_failure_streak_rechecks_the_engine(dirs, fake_helper, monkeypatch, caplog):
    """After REPROBE_AFTER_FAILURES unclassified failures in a row the cached
    probe is dropped: if the helper now reports on-device unusable, the audio
    is parked as unavailable (no helper run per chunk, nothing counted)."""
    audio, failed = dirs
    monkeypatch.setenv("MEETING_CAPTURE_STT", "apple")
    fake_helper.configure(default_rc=1)
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w._new_chunk(_chunk(audio, 1714003200), "meeting-a")
    w._new_chunk(_chunk(audio, 1714003210), "meeting-a")
    fake_helper.configure(clear_cache=False, probe_rc=75)      # e.g. the model was released
    with caplog.at_level("WARNING", logger="meeting-capture"):
        w._new_chunk(_chunk(audio, 1714003220), "meeting-a")   # third in a row: re-probe next time
    assert "3 transcriptions in a row failed" in caplog.text
    n = len(fake_helper.transcribe_calls())
    w._new_chunk(_chunk(audio, 1714003230), "meeting-a")
    assert len(fake_helper.transcribe_calls()) == n            # the probe said no: helper not run
    assert w.blocked and "isn't installed" in w.blocked
    parked = list(failed.glob("chunk-*.wav"))
    assert len(parked) == 4 and all(daemon._read_meta(p)["attempts"] == 0 for p in parked)


def test_a_file_that_fails_on_its_own_still_reaches_quarantine(dirs, fake_helper, monkeypatch):
    """The judge counts an unclassified failure once the next chunk shows the
    engine working — also when the bad file is the only one parked."""
    audio, failed = dirs
    monkeypatch.setenv("MEETING_CAPTURE_STT", "apple")
    bad = _chunk(audio, 1714003200)
    fake_helper.configure(transcribe_rc={bad.path.name: 1})
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w._new_chunk(bad, "meeting-a")
    for i in range(3):
        w._new_chunk(_chunk(audio, 1714003300 + i * 20), "meeting-a")   # works: the bad one counts
        if i < 2:
            assert daemon._read_meta(failed / bad.path.name)["attempts"] == i + 1
            _drain(w)                                    # session end: it fails again (held)
    assert (failed / "quarantine" / bad.path.name).exists()
    assert daemon._read_meta(failed / "quarantine" / bad.path.name)["attempts"] == 3
    assert _body("meeting-a").count("hello from this mac") == 3


def test_the_same_file_failing_twice_is_not_read_as_a_broken_engine(dirs, fake_helper, monkeypatch):
    """The session's last chunk fails, then fails again when it is retried at
    the session's end: that says nothing about the engine, so it is still
    counted once the next session's first chunk works."""
    audio, failed = dirs
    monkeypatch.setenv("MEETING_CAPTURE_STT", "apple")
    bad = _chunk(audio, 1714003300)
    fake_helper.configure(transcribe_rc={bad.path.name: 1})
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w._new_chunk(_chunk(audio, 1714003200), "meeting-a")
    w._new_chunk(bad, "meeting-a")
    _drain(w)
    assert len([c for c in fake_helper.transcribe_calls() if c[-1].endswith(bad.path.name)]) == 2
    assert daemon._read_meta(failed / bad.path.name)["attempts"] == 0
    w._new_chunk(_chunk(audio, 1714009000), "meeting-b")
    assert daemon._read_meta(failed / bad.path.name)["attempts"] == 1


def test_two_unclassified_failures_in_a_row_count_against_neither_file(dirs, monkeypatch):
    """Judge rule: two unclassified failures in a row (new or parked) are the
    engine — neither is counted, whatever works afterwards."""
    audio, failed = dirs
    results = iter([RuntimeError("a"), RuntimeError("b"), "fine"])

    def fake(path, role):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(daemon, "transcribe", fake)
    j = daemon._Judge()
    chunks = [_chunk(audio, 1714003200 + i * 20) for i in range(3)]
    for c in chunks:
        j.record(c, *daemon._attempt(c, "meeting-a"))
    assert [daemon._read_meta(failed / c.path.name)["attempts"] for c in chunks[:2]] == [0, 0]


def test_worker_downloads_a_missing_on_device_model_once(fake_helper, monkeypatch):
    from meeting_capture import transcriber
    fake_helper.configure(installed=[])      # supported, nothing installed: probe exits 75
    assert transcriber.resolve_backend().engine == "none"
    w = daemon.TranscriptionWorker()
    w._maybe_install_model()
    _wait(lambda: any("--install" in c for c in fake_helper.calls()) and not w._installing)
    w._maybe_install_model()                  # not again within the hour
    assert sum("--install" in c for c in fake_helper.calls()) == 1
    assert transcriber.resolve_backend().engine == "apple"


def test_worker_downloads_the_model_when_on_device_only_is_chosen(fake_helper, monkeypatch):
    fake_helper.configure(installed=[])
    monkeypatch.setenv("MEETING_CAPTURE_STT", "apple")
    w = daemon.TranscriptionWorker()
    w._maybe_install_model()
    _wait(lambda: any("--install" in c for c in fake_helper.calls()) and not w._installing)


def test_a_working_new_chunk_unblocks_the_worker(dirs, monkeypatch):
    from meeting_capture.transcriber import TranscriptionUnavailable
    audio, failed = dirs
    calls = []

    def fake(path, role):
        calls.append(path.name)
        if len(calls) == 1:
            raise TranscriptionUnavailable("no Gemini API key")
        return "now with a key"

    monkeypatch.setattr(daemon, "transcribe", fake)
    w = daemon.TranscriptionWorker()
    w._next_check = float("inf")
    w.start()
    w.submit(_chunk(audio, 1714003200), "meeting-a")
    _wait(lambda: w.blocked)
    w.submit(_chunk(audio, 1714003210), "meeting-a")
    _wait(lambda: not list(failed.glob("chunk-*.wav")))     # unblocked, and the parked one retried
    w.stop()
    assert w.blocked is None and _body("meeting-a").count("now with a key") == 2


def test_worker_never_downloads_a_model_when_gemini_is_chosen(fake_helper, monkeypatch):
    fake_helper.configure(installed=[])
    monkeypatch.setenv("MEETING_CAPTURE_STT", "gemini")
    daemon.TranscriptionWorker()._maybe_install_model()
    assert not any("--install" in c for c in fake_helper.calls())
