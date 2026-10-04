"""The channel guard reader: membership only, over every pipeline-monitor
contract fixture (vendored with their sha256 in tests/fixtures/channel_guard)."""
import hashlib
import json
from pathlib import Path

import pytest

from meeting_capture import channel_guard

FIXTURES = Path(__file__).parent / "fixtures" / "channel_guard"


def _pinned() -> dict[str, str]:
    out = {}
    for line in (FIXTURES / "PIN").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith(".json"):
            out[parts[1]] = parts[0]
    return out


def test_the_vendored_fixtures_are_the_pinned_ones():
    pinned = _pinned()
    on_disk = sorted(p.name for p in FIXTURES.glob("*.json"))
    assert on_disk == sorted(pinned) and len(pinned) >= 17
    for name, sha in pinned.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha, name


@pytest.mark.parametrize("name", sorted(_pinned()))
def test_fixture(name, tmp_path, monkeypatch):
    fx = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    marker = tmp_path / "channel.json"
    if "marker_raw" in fx:
        marker.write_text(fx["marker_raw"], encoding="utf-8")
    elif fx.get("marker") is not None:
        marker.write_text(json.dumps(fx["marker"], ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("CONTORCH_CHANNEL", raising=False)
    monkeypatch.delenv("CONTORCH_OP", raising=False)
    for k, v in fx["env"].items():
        monkeypatch.setenv(k, v)
    ok, msg = channel_guard.allowed(marker)
    expect = fx["expect"]
    assert (0 if ok else channel_guard.EXIT_BLOCKED) == expect["exit"], fx["description"]
    if "stderr" in expect:
        assert msg == expect["stderr"]
    if "stderr_contains" in expect:
        assert expect["stderr_contains"] in msg


@pytest.mark.parametrize("value,expect", [(None, "dev"), ("", "dev"), ("app", "app"), ("brew", "brew"),
                                          ("dev", "dev"), ("nightly", "dev"), ("APP", "dev")])
def test_channel_comes_only_from_the_environment(value, expect, monkeypatch):
    if value is None:
        monkeypatch.delenv("CONTORCH_CHANNEL", raising=False)
    else:
        monkeypatch.setenv("CONTORCH_CHANNEL", value)
    assert channel_guard.channel() == expect


def test_the_default_marker_is_under_home():
    assert channel_guard.marker_path().parts[-2:] == (".contorch", "channel.json")


def test_no_path_heuristics_in_channel():
    """$CONTORCH_CHANNEL is the only channel source (no sys.executable / bundle
    path guessing anywhere in the package)."""
    src = Path(channel_guard.__file__).parent
    for py in src.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "CONTORCH_CHANNEL" in line and "environ" in line:
                assert py.name == "channel_guard.py", f"{py.name} reads $CONTORCH_CHANNEL itself: {line}"
