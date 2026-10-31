"""Source restoration uses actual state/queue responses, including asynchronous failures."""

import asyncio
import copy

import pytest

from relay_rig import until
from soloify import wire
from soloify.player import Player
from soloify.rewind import RestoreError, Rewind, Timeline
from soloify.state import State
from test_audio import Rig, _settle
from test_reliability import Writer


class Source:
    def __init__(self, player, tracks=("a",), index=None):
        self.player = player
        self.tracks = tracks
        self.index = len(tracks) - 1 if index is None else index
        self.word = "playing"
        self.position = 12000
        self.context = "spotify:playlist:context"
        self.shuffle = True
        self.repeat = "context"
        self.calls = []
        self.refuse = None
        self.confirm = True
        self.disconnect = False
        self.delay = 0
        self.serial = 1_000_000
        self.emit()

    def event(self):
        return {
            "type": "playback_state",
            "status": self.word,
            "is_active": True,
            "item": {
                "uri": f"spotify:track:{self.tracks[self.index]}",
                "decorations": {"playback": {"duration_ms": 180000}},
            },
            "context": {"uri": self.context},
            "position": {
                "position_ms": self.position,
                "timestamp_ms": self.serial,
                "speed": 1 if self.word == "playing" else 0,
            },
            "options": {"shuffle": self.shuffle, "repeat": self.repeat},
        }

    def emit(self):
        self.player.clock = lambda: self.serial
        self.player.event(self.event())

    def entry(self, index):
        return {
            "uid": f"entry{index}",
            "source": "context",
            "item": {"uri": f"spotify:track:{self.tracks[index]}"},
        }

    async def request(self, name, **fields):
        if self.disconnect:
            raise ConnectionError("disconnected")
        await asyncio.sleep(self.delay)
        if name == "get_state":
            result = self.event()
            self.player.event(result)
            return result
        assert name == "get_queue"
        return {
            "previous": [self.entry(i) for i in range(self.index - 1, -1, -1)],
            "upcoming": [self.entry(i) for i in range(self.index + 1, len(self.tracks))],
        }

    async def command(self, name, **fields):
        self.calls.append((name, fields))
        if self.disconnect or name == self.refuse:
            return False
        if self.confirm:
            if name == "pause":
                self.word = "paused"
            elif name == "play":
                self.word = "playing"
            elif name == "seek":
                self.position = fields["position_ms"]
            elif name == "skip_next":
                self.index += 1
                self.word = "playing"
                self.position = 0
            elif name == "skip_prev":
                self.index -= 1
                self.word = "playing"  # Soloist can auto-play on skip
                self.position = 0
            self.emit()
        return True


def setup_source(tracks=("a",)):
    player = Player()
    source = Source(player, tracks, 0)
    player.ws = source
    first = player.timeline.current
    for index in range(1, len(tracks)):
        source.index = index
        source.serial += 10000
        source.position = 0
        source.emit()
    rewind = Rewind(player)
    rewind.remember(b"\0\0\0\1" + f"token={first.token}\nposition=12500\n".encode())
    return player, source, rewind


async def test_seek_restores_paused_position_and_preserves_context_options():
    player, source, rewind = setup_source()
    source.delay = 0.001
    await rewind.restore()
    assert source.position == 12500 and source.word == "paused"
    assert player.explicit_paused and not player.paused_by_us
    assert source.context == "spotify:playlist:context"
    assert player.state.shuffle and player.state.repeat == "context"
    assert not rewind.bookmark.failed


async def test_repeat_track_without_queue_movement_cannot_select_an_older_duplicate():
    player, source, rewind = setup_source(("a", "a"))
    source.repeat = "track"
    source.serial += 12000
    source.position = 12000
    source.emit()
    heard = player.timeline.current
    rewind.remember(b"\0\0\0\x0b" + f"token={heard.token}\nposition=12500\n".encode())
    source.serial += 180000
    source.position = 0
    source.emit()  # a new repeat lap, with the SAME queue entry still current
    assert player.timeline.current is not heard
    with pytest.raises(RestoreError, match="ambiguous"):
        await rewind.restore()
    assert source.index == 1
    assert not any(name == "skip_prev" for name, _ in source.calls)


@pytest.mark.parametrize("action", [b"S50000", b"n", b"browse"])
async def test_new_navigation_supersedes_delayed_restoration(action):
    rig = Rig(limit=1000)
    await _settle()
    player = rig.session.player
    source = player.ws = Source(player, ("a", "b"), 0)
    source.delay = 0.08
    writer = rig.session.writer = Writer()
    token = player.timeline.current.token
    payload = b"\0\0\0\7" + f"token={token}\nposition=12500\n".encode()
    task = None
    try:
        await rig.send(wire.BOOKMARK, payload)
        await until(lambda: source.calls)
        if action == b"browse":

            async def prepare():
                async def play():
                    await source.command("skip_next")

                return play

            task = asyncio.create_task(rig.session.browse_boundary(prepare))
            await _settle()
        else:
            await rig.send(wire.CONTROL, action)
        await until(lambda: not rig.session.control_tasks)
        if task:
            await task
        assert ("seek", {"position_ms": 12500}) not in source.calls
        assert not rig.session.rewind.bookmark
        if action == b"S50000":
            assert source.position == 50000
        else:
            assert source.index == 1
        assert wire.frame(wire.BOUNDARY, b"Q" if action == b"browse" else action) in writer.data
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_latest_pause_aborts_play_waiting_for_capture_without_timeout():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    player = rig.session.player
    source = player.ws = Source(player)
    rig.session.writer = Writer()
    token = player.timeline.current.token
    payload = b"\0\0\0\x08" + f"token={token}\nposition=12500\n".encode()
    try:
        await rig.send(wire.BOOKMARK, payload)
        await until(lambda: not rig.session.control_tasks)
        bookmark = player.rewind.bookmark
        await rig.send(wire.CONTROL, b"P")
        await until(lambda: len(rig.encoders) == 2)
        # No capture packet arrives: the newer Pause must wake the readiness wait.
        await rig.send(wire.BOOKMARK, payload)
        await until(lambda: not rig.session.control_tasks, timeout=1)
        assert source.word == "paused" and not rig.session.streaming
        assert player.rewind.bookmark is bookmark
        assert all(encoder.stopped for encoder in rig.encoders)
        assert not any(name == "play" for name, _ in source.calls)
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_reconnect_play_primes_bookmark_without_an_earlier_audio_request():
    rig = Rig(limit=1000)
    await _settle()
    player = rig.session.player
    source = player.ws = Source(player)
    rig.session.writer = Writer()
    token = player.timeline.current.token
    payload = b"\0\0\0\x0c" + f"token={token}\nposition=12500\n".encode()
    try:
        await rig.send(wire.BOOKMARK, payload)
        await until(lambda: not rig.session.control_tasks)
        assert rig.session.audio_request is None
        await rig.send(wire.CONTROL, b"P")
        await until(lambda: rig.encoders, timeout=1)
        assert source.word == "paused"
        await rig.encoders[-1].queue.put(b"warmup")
        await until(lambda: not rig.session.control_tasks)
        assert source.word == "playing" and rig.session.streaming
        assert player.rewind.bookmark is None
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


@pytest.mark.parametrize("phone", [False, True])
async def test_pause_supersedes_dispatched_play_and_keeps_recoverable_bookmark(phone):
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    player = rig.session.player
    source = player.ws = Source(player)
    rig.session.writer = Writer()
    token = player.timeline.current.token
    payload = b"\0\0\0\x09" + f"token={token}\nposition=12500\n".encode()
    dispatched, release = asyncio.Event(), asyncio.Event()
    command = source.command

    async def delayed_play(name, **fields):
        if name == "play":
            dispatched.set()
            await release.wait()
        return await command(name, **fields)

    source.command = delayed_play
    try:
        await rig.send(wire.BOOKMARK, payload)
        await until(lambda: not rig.session.control_tasks)
        bookmark = player.rewind.bookmark
        await rig.send(wire.CONTROL, b"P")
        await until(lambda: len(rig.encoders) == 2)
        await rig.encoders[-1].queue.put(b"warmup")
        await asyncio.wait_for(dispatched.wait(), 1)
        if phone:
            # The source is already paused while Play's dispatch response is late.
            player.event({"type": "playback_changed", "status": "paused"})
        else:
            await rig.send(wire.BOOKMARK, payload)
        release.set()
        await until(lambda: not rig.session.control_tasks, timeout=1)
        assert source.word == "paused" and not rig.session.streaming
        assert player.rewind.bookmark is bookmark
        assert all(encoder.stopped for encoder in rig.encoders)
    finally:
        release.set()
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


@pytest.mark.parametrize("intermediate", ["stale_query", "buffering"])
async def test_intermediate_play_states_do_not_become_phone_pause(intermediate):
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    player = rig.session.player
    source = player.ws = Source(player)
    writer = rig.session.writer = Writer()
    token = player.timeline.current.token
    payload = b"\0\0\0\x0a" + f"token={token}\nposition=12500\n".encode()
    request = source.request
    command = source.command
    stale = True

    async def delayed_state(name, **fields):
        nonlocal stale
        if (
            intermediate == "stale_query"
            and name == "get_state"
            and source.word == "playing"
            and stale
        ):
            stale = False
            event = source.event()
            event["status"] = "paused"
            event["position"]["speed"] = 0
            player.event(event)
            return event
        return await request(name, **fields)

    async def buffering_play(name, **fields):
        nonlocal stale
        if intermediate == "buffering" and name == "play":
            stale = False
            player.event({"type": "playback_changed", "status": "buffering"})
        return await command(name, **fields)

    source.request = delayed_state
    source.command = buffering_play
    try:
        await rig.send(wire.BOOKMARK, payload)
        await until(lambda: not rig.session.control_tasks)
        writer.data.clear()
        await rig.send(wire.CONTROL, b"P")
        await until(lambda: len(rig.encoders) == 2)
        await rig.encoders[-1].queue.put(b"warmup")
        await until(lambda: not rig.session.control_tasks)
        assert not stale and source.word == "playing"
        assert rig.session.streaming and player.rewind.bookmark is None
        assert wire.frame(wire.PAUSE_NOW) not in writer.data
        assert rig.session.capture.release.is_set()
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_browse_play_ignores_earlier_pause_notifications_during_mutation():
    rig = Rig(limit=1000)
    await _settle()
    player = rig.session.player
    source = player.ws = Source(player)
    writer = rig.session.writer = Writer()
    rig.session.capture_blocked = True

    async def prepare():
        async def action():
            await source.command("play")
            # The old Pause's fade notification can follow the newer API Play.
            player.event({"type": "playback_changed", "status": "paused"})

        return action

    try:
        await rig.session.browse_boundary(prepare)
        await until(lambda: not rig.session.control_tasks)
        assert source.word == "playing" and not player.explicit_paused
        assert not rig.session.capture_blocked
        assert wire.frame(wire.PAUSE_NOW) not in writer.data
        assert wire.frame(wire.BOUNDARY, b"Q") in writer.data
        assert not rig.session.mutating and not player.deferred
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


@pytest.mark.parametrize("tracks", [("a", "b"), ("a", "b", "a")])
async def test_restores_heard_occurrence_through_verified_previous_history(tracks):
    player, source, rewind = setup_source(tracks)
    await rewind.restore()
    assert source.index == 0
    assert source.position == 12500 and source.word == "paused"
    assert len([name for name, _ in source.calls if name == "skip_prev"]) == len(tracks) - 1
    assert player.timeline.current.token == rewind.bookmark.occurrence.token


@pytest.mark.parametrize("failure", ["refused", "timeout", "disconnect", "missing", "context"])
async def test_restore_failure_retains_bookmark_and_explicit_play_can_retry(failure):
    player, source, rewind = setup_source(("a", "b"))
    rewind.TIMEOUT = 0.03
    original = source.request
    if failure == "refused":
        source.refuse = "seek"
    elif failure == "timeout":
        source.confirm = False
    elif failure == "disconnect":
        source.disconnect = True
    elif failure == "context":
        player.timeline.current.state.context = "changed"
    else:

        async def request(name, **fields):
            return {"previous": []} if name == "get_queue" else await original(name, **fields)

        source.request = request
    bookmark = rewind.bookmark
    with pytest.raises(RestoreError):
        await rewind.restore()
    assert rewind.bookmark is bookmark and bookmark.failed
    assert player.explicit_paused and not player.timeline.restoring
    source.refuse = None
    source.confirm = True
    source.disconnect = False
    source.request = original
    player.timeline.current.state.context = source.context
    await rewind.restore()
    assert source.position == 12500 and source.index == 0


async def test_dispatch_ack_without_state_confirmation_is_not_success():
    _, source, rewind = setup_source()
    source.confirm = False
    rewind.TIMEOUT = 0.02
    with pytest.raises(RestoreError):
        await rewind.restore()
    assert not [call for call in source.calls if call[0] == "seek"]


async def test_cancellation_keeps_bookmark_and_releases_restore_guard():
    player, source, rewind = setup_source()
    source.delay = 10
    task = asyncio.create_task(rewind.restore())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rewind.bookmark and player.explicit_paused and not player.timeline.restoring


def test_sample_anchors_use_capture_clock_across_boundary_and_repeat():
    player, source, _ = setup_source(("a", "a", "b"))
    first = player.timeline.anchor(1_000_050)
    second = player.timeline.anchor(1_010_020)
    third = player.timeline.anchor(1_020_000)
    assert b"track=a\n" in first and b"position=12050\n" in first
    assert b"token=1\n" in first and b"token=2\n" in second
    assert b"track=b\n" in third and b"position=0\n" in third
    assert player.timeline.anchor(999_999) is None


async def test_session_pause_failure_then_play_retries_and_primes_before_source_play():
    rig = Rig(limit=1000)
    await _settle()
    source = Source(rig.session.player)
    rig.session.player.ws = source
    writer = rig.session.writer = Writer()
    token = rig.session.player.timeline.current.token
    bookmark = b"\0\0\0\4" + f"token={token}\nposition=12500\n".encode()
    source.refuse = "seek"
    try:
        await rig.send(wire.AUDIO_REQUEST, b"1")
        await rig.send(wire.BOOKMARK, bookmark)
        await until(lambda: any(d[:1] == wire.RESTORED for d in writer.data))
        assert wire.frame(wire.RESTORED, bookmark[:4] + b"\0Source refused seek") in writer.data
        assert not rig.session.streaming and rig.session.rewind.bookmark
        for _ in range(2):
            await rig.send(wire.AUDIO_REQUEST, b"1")
        assert len(rig.encoders) == 1
        source.refuse = None
        await rig.send(wire.CONTROL, b"P")
        await until(lambda: len(rig.encoders) == 2)
        assert not [c for c in source.calls if c[0] == "play"]
        await rig.encoders[-1].queue.put(b"warmup")
        await until(lambda: any(c[0] == "play" for c in source.calls))
        await until(lambda: not rig.session.rewind.bookmark)
        assert source.position == 12500 and source.word == "playing"
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_phone_pause_sends_stop_before_waiting_on_session_lock():
    rig = Rig(limit=1000)
    await _settle()
    writer = rig.session.writer = Writer()
    await rig.session.lock.acquire()
    try:
        rig.session.player.event({"type": "playback_changed", "status": "paused"})
        assert writer.data == [wire.frame(wire.PAUSE_NOW)]
        rig.session.player.event({"type": "playback_changed", "status": "playing"})
        assert writer.data[-1] == wire.frame(wire.PAUSE_NOW, b"P")
        assert not rig.encoders
    finally:
        rig.session.lock.release()
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


def test_missing_source_anchor_never_guesses_a_bookmark():
    timeline = Timeline()
    state = State()
    assert timeline.anchor(100) is None
    assert timeline.known_start(state, 100) == b"state=none\n"
    # A copy of an event cannot mutate an already-issued occurrence's identity.
    player, source, _ = setup_source()
    before = copy.copy(player.timeline.current.state)
    source.context = "elsewhere"
    source.emit()
    assert before.context != source.context


async def test_real_relay_restores_startup_bookmark_then_refills_fresh(tmp_path):
    import tempfile
    from pathlib import Path

    from relay_rig import Relay
    from soloify import audio, server
    from test_audio import FakeEncoder
    from test_contract import children, controls, read_state

    player = Player()
    source = player.ws = Source(player)
    encoders, sessions = [], []

    def encoder():
        capture = FakeEncoder()
        # The first capture presents the opening anchor. Resume exercises the
        # timestamp-less decoder warmup fallback used by the test encoder.
        if not encoders:
            capture.capture_at = source.serial / 1000
        encoders.append(capture)
        return capture

    async def session(reader, writer):
        active = audio.Session(player, encoder, 1000)
        sessions.append(active)
        await active.run(reader, writer)

    identity = server.Identity.load(tmp_path / "bridge")
    bridge = server.Server(identity, session)
    listener = await bridge.start("127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        relay = await Relay(
            tmp_path,
            [f"127.0.0.1 {port} {identity.token} sha256={identity.pin}"],
            "-q",
            "-b",
            "1000",
            env={"SOLOIFY_Q2POD": str(base)},
        ).start()
        try:
            await until(lambda: bool(encoders))
            for _ in range(5):
                await encoders[0].queue.put(b"packet")
            await until(lambda: sessions[0].sent == 5)
            controls(base, b"s")
            await until(lambda: sessions[0].rewind.bookmark is not None)
            await until(lambda: not sessions[0].mutating)
            bookmark = sessions[0].rewind.bookmark
            assert bookmark.position == 12000 and not bookmark.failed
            assert source.word == "paused" and source.position == 12000
            assert "position=12000\n" in read_state(base)
            assert children(relay.proc.pid) == []
            controls(base, b"P")
            await until(lambda: len(encoders) == 2)
            assert source.word == "paused"
            await encoders[1].queue.put(b"warmup")
            await until(lambda: source.word == "playing")
            for _ in range(60):
                await encoders[1].queue.put(b"fresh")
            await until(lambda: len(children(relay.proc.pid)) == 2)
            assert not sessions[0].rewind.bookmark
            assert not Path(f"{base}.pause-error").exists()
        finally:
            await relay.stop()
            listener.close()
            if bridge.current:
                bridge.current.cancel()
                await asyncio.gather(bridge.current, return_exceptions=True)
            await listener.wait_closed()


async def test_reconnect_snapshot_does_not_retry_failed_bookmark_or_play():
    rig = Rig(limit=1000)
    await _settle()
    source = Source(rig.session.player)
    rig.session.player.ws = source
    writer = rig.session.writer = Writer()
    token = rig.session.player.timeline.current.token
    payload = b"\0\0\0\5" + f"token={token}\nposition=12500\n".encode()
    source.refuse = "seek"
    try:
        await rig.session.pause_bookmark(payload)
        bookmark = rig.session.rewind.bookmark
        assert bookmark.failed
        source.refuse = None
        source.calls.clear()
        writer.data.clear()
        rig.session.player.event({"type": "gone"})
        rig.session.player.event({"type": "auth_state", "logged_in": True, "is_active": True})
        source.word = "playing"
        source.emit()  # first full state on a new source connection
        await _settle()
        assert wire.frame(wire.PAUSE_NOW, b"P") not in writer.data
        assert not source.calls
        source.word = "paused"
        source.emit()
        writer.data.clear()
        source.word = "playing"
        source.emit()  # a later queried snapshot is still not an explicit phone Play
        assert wire.frame(wire.PAUSE_NOW, b"P") not in writer.data
        await rig.session.pause_bookmark(payload)  # relay reconnect re-sends its bookmark
        assert rig.session.rewind.bookmark is bookmark
        assert not any(name == "seek" for name, _ in source.calls)
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_phone_play_during_pause_shutdown_is_ordered_after_bookmark():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    source = Source(rig.session.player)
    rig.session.player.ws = source
    writer = rig.session.writer = Writer()
    token = rig.session.player.timeline.current.token
    payload = b"\0\0\0\6" + f"token={token}\nposition=12500\n".encode()
    stopping, release = asyncio.Event(), asyncio.Event()
    stop = rig.encoders[0].stop

    async def slow_stop():
        stopping.set()
        await release.wait()
        await stop()

    rig.encoders[0].stop = slow_stop
    task = asyncio.create_task(rig.session.pause_bookmark(payload))
    try:
        await stopping.wait()
        source.word = "playing"  # the phone pressed Play while encoder shutdown holds the lock
        rig.session.player.event({"type": "playback_changed", "status": "playing"})
        assert wire.frame(wire.PAUSE_NOW, b"P") in writer.data
        release.set()
        await task
        assert source.word == "paused"  # relay's following c P will perform the ordered resume
        assert not rig.session.streaming
    finally:
        release.set()
        task.cancel()
        rig.task.cancel()
        await asyncio.gather(task, rig.task, return_exceptions=True)


def test_late_track_position_anchor_replaces_unconfirmed_transition():
    player, source, _ = setup_source()
    source.serial += 1000
    player.event({"type": "track_changed", "item": {"uri": "spotify:track:b"}})
    assert player.timeline.anchor(source.serial) is None
    player.event(
        {
            "type": "position_sync",
            "position": {
                "position_ms": 20,
                "timestamp_ms": source.serial - 10,
                "speed": 1,
            },
        }
    )
    anchor = player.timeline.anchor(source.serial)
    assert b"track=b\n" in anchor and b"position=30\n" in anchor


async def test_retry_retains_verified_navigation_progress():
    player, source, rewind = setup_source(("a", "b", "c"))
    original = source.command

    async def refuse_final_seek(name, **fields):
        if name == "seek" and fields.get("position_ms") == 12500:
            return False
        return await original(name, **fields)

    source.command = refuse_final_seek
    with pytest.raises(RestoreError):
        await rewind.restore()
    assert source.index == 0 and rewind.bookmark.route == []
    assert player.timeline.current is rewind.bookmark.occurrence
    source.command = original
    await rewind.restore()
    assert source.position == 12500
    # A new traversal follows the restored occurrence, excluding abandoned future laps.
    source.index = 1
    source.word = "playing"
    source.position = 0
    source.serial += 10000
    source.emit()
    token = rewind.bookmark.occurrence.token
    rewind.bookmark = None
    rewind.remember(b"\0\0\0\2" + f"token={token}\nposition=12500\n".encode())
    await rewind.restore()
    assert source.index == 0 and source.position == 12500


async def test_oldest_first_live_queue_order_is_verified_before_navigation():
    _, source, rewind = setup_source(("a", "b", "c"))
    original = source.request

    async def oldest_first(name, **fields):
        result = await original(name, **fields)
        if name == "get_queue":
            result["previous"].reverse()
        return result

    source.request = oldest_first
    await rewind.restore()
    assert source.index == 0 and source.position == 12500
    assert source.word == "paused"


@pytest.mark.parametrize("tracks", [("a", "b", "a", "c"), ("a", "a", "c")])
async def test_ambiguous_queue_orientation_stays_paused(tracks):
    _, source, rewind = setup_source(tracks)
    # Bookmark the most recent a, so either end has the same URI but different UIDs.
    occurrence = list(rewind.player.timeline.occurrences)[-2]
    rewind.remember(b"\0\0\0\1" + f"token={occurrence.token}\nposition=100\n".encode())
    with pytest.raises(RestoreError, match="orientation is ambiguous"):
        await rewind.restore()
    assert source.index == len(tracks) - 1 and source.word == "paused"


async def test_previous_can_pass_queued_target_then_verified_next_restores_it():
    player, source, rewind = setup_source(("a", "b", "c"))
    target = list(player.timeline.occurrences)[1]
    rewind.remember(b"\0\0\0\2" + f"token={target.token}\nposition=1234\n".encode())
    original = source.command

    async def previous_to_context(name, **fields):
        if name == "skip_prev" and source.index == 2:
            source.index = 1  # the command below will pass the queued b and visit context a
        return await original(name, **fields)

    source.command = previous_to_context
    await rewind.restore()
    assert source.index == 1 and source.position == 1234 and source.word == "paused"
    assert [name for name, _ in source.calls if name.startswith("skip_")] == [
        "skip_prev",
        "skip_next",
    ]


async def test_restore_preserves_current_options_changed_since_track_started():
    player, source, rewind = setup_source(("a", "b"))
    source.shuffle = False
    source.repeat = "track"
    source.emit()
    await rewind.restore()
    assert source.index == 0 and source.word == "paused"
    assert not player.state.shuffle and player.state.repeat == "track"
    assert not any(name.startswith("set_") for name, _ in source.calls)
