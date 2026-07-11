from meeting_capture import mic


def test_is_mic_active_returns_bool():
    assert isinstance(mic.is_mic_active(), bool)


def test_mic_name_returns_str_or_none():
    name = mic.mic_name()
    assert name is None or isinstance(name, str)


def test_active_mic_name_returns_str_or_none():
    name = mic.active_mic_name()
    assert name is None or isinstance(name, str)


def test_all_device_ids_returns_list():
    ids = mic._all_device_ids()
    assert isinstance(ids, list)
    assert all(isinstance(i, int) for i in ids)


def test_handles_missing_coreaudio(monkeypatch):
    """If CoreAudio framework can't be loaded (e.g. non-macOS), is_mic_active returns False."""
    monkeypatch.setattr(mic, "_CA", None)
    assert mic.is_mic_active() is False
    assert mic.mic_name() is None
    assert mic.active_mic_name() is None
    assert mic._all_device_ids() == []
    assert mic._process_object_ids() == []


# ---------------------------------------------------------------------------
# Process-level attribution: our own SCK mic capture (attributed to replayd)
# must not flip the gate; a real app holding the mic must.


def _patch_processes(monkeypatch, procs: dict[int, tuple[bool, str | None]]):
    """procs: {object_id: (is_running_input, bundle_id)}"""
    monkeypatch.setattr(mic, "_process_object_ids", lambda: list(procs))
    monkeypatch.setattr(mic, "_process_is_running_input", lambda o: procs[o][0])
    monkeypatch.setattr(mic, "_process_bundle_id", lambda o: procs[o][1])


def test_replayd_input_does_not_trip_gate(monkeypatch):
    _patch_processes(monkeypatch, {10: (True, "com.apple.replayd"), 11: (False, "us.zoom.xos")})
    assert mic.is_mic_active() is False


def test_real_app_input_trips_gate(monkeypatch):
    _patch_processes(monkeypatch, {10: (True, "com.apple.replayd"), 11: (True, "us.zoom.xos")})
    assert mic.is_mic_active() is True


def test_unknown_bundle_input_trips_gate(monkeypatch):
    # A process with no bundle id (bare binary) still counts as a real consumer.
    _patch_processes(monkeypatch, {10: (True, None)})
    assert mic.is_mic_active() is True


def test_no_input_running_means_inactive(monkeypatch):
    _patch_processes(monkeypatch, {10: (False, "us.zoom.xos"), 11: (False, None)})
    assert mic.is_mic_active() is False


def test_falls_back_to_device_check_without_process_objects(monkeypatch):
    monkeypatch.setattr(mic, "_process_object_ids", lambda: [])
    monkeypatch.setattr(mic, "_all_device_ids", lambda: [7])
    monkeypatch.setattr(mic, "_has_input_streams", lambda d: True)
    monkeypatch.setattr(mic, "_is_device_running", lambda d: True)
    assert mic.is_mic_active() is True
