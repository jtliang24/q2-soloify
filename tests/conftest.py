"""Settings every test's relay inherits through os.environ."""

import pytest


@pytest.fixture(autouse=True)
def relay_paths(monkeypatch, tmp_path):
    # A relay with no remote pairs by itself: its discovery stays on loopback, at a port nothing
    # answers on, never the real network's broadcast address; tests that pair set their own
    monkeypatch.setenv("SOLOIFY_PAIR_BROADCAST", "127.0.0.1")
    monkeypatch.setenv("SOLOIFY_PAIR_UDP_PORT", "9")
    # The relay's WAN failure count, /tmp/q2-soloify.away on the device
    monkeypatch.setenv("SOLOIFY_AWAY_COUNT", str(tmp_path / "away-count"))
