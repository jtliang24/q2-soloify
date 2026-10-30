"""Device timing regressions, with real relay sinks and controlled encoder/ACK clocks."""

import asyncio

import pytest

from relay_rig import until
from soloify import audio, browse, spotify, wire
from test_audio import Rig, _settle
from test_browse import API, Local, request
from test_contract import children, controls, read_state, state
from test_contract import contract as contract


class Writer:
    def __init__(self):
        self.data = []

    def write(self, data):
        self.data.append(data)

    def is_closing(self):
        return False


@pytest.mark.parametrize(
    "failure", ["invalid", "missing", "unavailable", "liked", "queue", "queue_race"]
)
async def test_preparation_failure_preserves_capture(tmp_path, failure):
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    session = rig.session
    session.writer = Writer()
    api = API()
    session.browser = browse.Browser(api, session.player, tmp_path)
    args = ("play", "invalid")
    if failure != "invalid":
        args = ("play", "spotify:track:T")
    if failure in ("unavailable", "liked"):
        folder = tmp_path / "soloist-state"
        folder.mkdir()
        (folder / ".device_id").write_text("device")
        api.replies.append({"devices": [] if failure == "unavailable" else [{"id": "device"}]})
    if failure == "liked":
        args = ("play", "spotify:user:U:collection", "0", "spotify:track:T")
    if failure in ("queue", "queue_race"):
        session.player.ws = Local()
        args = ("skip", "0", "spotify:track:stale" if failure == "queue" else "spotify:track:0")
        if failure == "queue_race":
            local = session.player.ws
            original = local.request

            async def changed(name, **kw):
                if local.calls:
                    local.upcoming[0] = {"uid": "phone", "item": {"uri": "spotify:track:0"}}
                return await original(name, **kw)

            local.request = changed
    try:
        await session.browse(b"\0\0\0\1" + request(*args))
        assert session.streaming and not rig.encoders[0].stopped
        assert all(data[:1] not in (b"B", wire.BOUNDARY) for data in session.writer.data)
        assert not any(method == "PUT" for method, _, _ in api.calls)
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["error", "timeout", "disconnect", "cancel"])
async def test_dispatched_failure_completes_boundary(tmp_path, failure):
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    s = rig.session
    s.writer = Writer()
    started = asyncio.Event()

    async def prepare():
        async def mutate():
            started.set()
            if failure == "cancel":
                await asyncio.Event().wait()
            raise {
                "error": spotify.APIError("refused", "Rejected"),
                "timeout": TimeoutError(),
                "disconnect": ConnectionError(),
            }[failure]

        return mutate

    task = asyncio.create_task(s.browse_boundary(prepare, True, b"\0\0\0\1"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if failure == "cancel":
            task.cancel()
        with pytest.raises(
            (spotify.APIError, TimeoutError, ConnectionError, asyncio.CancelledError)
        ):
            await task
        assert rig.encoders[0].stopped
        assert [d[:1] for d in s.writer.data].count(b"B") == 1
        assert wire.frame(wire.BOUNDARY, b"Q") in s.writer.data
        assert not s.player.deferred
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_browse_forwarding_does_not_stop_sink_before_reset_begin(contract):
    from test_contract import browse as call

    relay, peer, base = contract
    await peer.packets(100)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    before = children(relay.proc.pid)
    task = asyncio.create_task(call(base, request("play", "invalid")))
    await until(lambda: any(k == wire.BROWSE_REQUEST for k, _ in peer.frames))
    correlation = next(p[:4] for k, p in peer.frames if k == wire.BROWSE_REQUEST)
    await asyncio.sleep(0.15)
    assert children(relay.proc.pid) == before
    await peer.send(wire.BROWSE_RESPONSE, correlation + browse.response("7", "bad", "Invalid"))
    assert b"\tbad\t" in await task
    assert children(relay.proc.pid) == before
    await peer.send(b"B", correlation)
    await until(lambda: not children(relay.proc.pid))
    await peer.packets(20)  # old encoder tail remains discarded
    await peer.send(wire.BOUNDARY, b"Q")
    await peer.send(wire.STATE, state("fresh"))
    await peer.packets(20)
    await until(lambda: "track=fresh\n" in read_state(base))


async def test_external_resume_waits_for_new_capture(contract):
    relay, peer, base = contract
    await peer.packets(40)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    await peer.send(wire.STATE, state(word="paused"))
    await peer.send(wire.BOUNDARY, b"s")  # external pause capture has drained
    await until(lambda: not children(relay.proc.pid))
    await peer.send(wire.STATE, state())
    await asyncio.sleep(0.35)
    assert children(relay.proc.pid) == []
    await peer.send(wire.CAPTURE, (2).to_bytes(4, "big"))
    await peer.packets(12)
    await until(lambda: len(children(relay.proc.pid)) == 2)


async def test_double_previous_preserves_both_boundaries(contract):
    relay, peer, base = contract
    await peer.packets(40)
    controls(base, b"b", b"b")
    await until(lambda: peer.controls() == [b"b", b"b"])
    await peer.send(wire.BOUNDARY, b"b")
    await peer.send(wire.STATE, state("intermediate"))
    await peer.packets(30)
    await asyncio.sleep(0.1)
    assert not children(relay.proc.pid)
    await peer.send(wire.BOUNDARY, b"b")
    await peer.send(wire.STATE, state("previous"))
    await peer.packets(20)
    await until(lambda: "track=previous\n" in read_state(base))


@pytest.mark.parametrize("ack_delay", [0.001, 0.15])
async def test_encoder_burst_yields_to_healthy_or_delayed_acks(ack_delay):
    rig = Rig(limit=300)
    await rig.send(wire.AUDIO_REQUEST, b"1")

    async def acks():
        while rig.session.sent < 200:
            await asyncio.sleep(ack_delay)
            await rig.send(wire.ACK, rig.session.sent.to_bytes(4, "big"))

    task = asyncio.create_task(acks())
    try:
        await rig.packets(200)
        await asyncio.wait_for(task, 4)
        assert rig.session.sent == 200
        assert rig.session.streaming and not rig.encoders[0].stopped
        assert rig.soloist.calls == []
        assert not rig.session.tasks
    finally:
        task.cancel()
        rig.task.cancel()
        await asyncio.gather(task, rig.task, return_exceptions=True)


async def test_stall_burst_has_one_shutdown_and_bounded_tail():
    rig = Rig(limit=100)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    try:
        await rig.packets(1000)
        await until(lambda: rig.encoders[0].stopped)
        await _settle()
        assert rig.soloist.calls == [("pause",)]
        assert rig.session.sent <= 5 + 15
        assert not rig.session.tasks
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_old_shutdown_cannot_stop_replacement_capture():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    s = rig.session
    old = s.generation
    await s.lock.acquire()
    delayed = asyncio.create_task(s.halt("pause", old))
    try:
        await s._halt("control")
        await s._want()
        s.lock.release()
        await delayed
        assert s.generation == old + 1 and s.streaming
        assert not rig.encoders[-1].stopped
    finally:
        if s.lock.locked():
            s.lock.release()
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_explicit_pause_survives_browser_reopen_and_recovery_probes():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    try:
        await rig.session.halt("stall")
        assert rig.session.player.paused_by_us
        # An explicit pause cancels recovery ownership, even if already paused.
        await rig.send(wire.CONTROL, b"s")
        await rig.send(wire.BROWSE_REQUEST, b"\0\0\0\1" + request("caps"))
        for _ in range(3):
            await rig.send(wire.AUDIO_REQUEST, b"1")
        assert rig.session.player.explicit_paused
        assert rig.soloist.calls == [("pause",), ("pause",)]
        assert not rig.session.streaming
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_partial_queue_execution_still_finishes_reset(tmp_path):
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    s = rig.session
    local = Local()
    original = local.request

    async def mutate_queue(name, **kw):
        result = await original(name, **kw)
        if name == "skip_next":
            local.upcoming[0] = {"uid": "phone", "item": {"uri": "spotify:track:phone"}}
        return result

    local.request = mutate_queue
    s.player.ws = local
    s.browser = browse.Browser(None, s.player, tmp_path)
    s.writer = Writer()
    try:
        await s.browse(b"\0\0\0\1" + request("skip", "2", "spotify:track:2"))
        assert len([c for c in local.calls if c[0] == "skip_next"]) == 1
        assert s.writer.data[0] == wire.frame(wire.RESET_BEGIN, b"\0\0\0\1")
        assert wire.frame(wire.BOUNDARY, b"Q") in s.writer.data
        assert b"\tunavailable\t" in s.writer.data[-1]
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_cancellation_during_drain_waits_before_completing_reset():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    s = rig.session
    s.writer = Writer()
    draining, release = asyncio.Event(), asyncio.Event()
    encoder = rig.encoders[0]
    stop = encoder.stop
    mutations = []

    async def slow_stop():
        draining.set()
        await release.wait()
        await stop()

    async def prepare():
        async def mutate():
            mutations.append(True)

        return mutate

    encoder.stop = slow_stop
    task = asyncio.create_task(s.browse_boundary(prepare, True, b"\0\0\0\1"))
    try:
        await asyncio.wait_for(draining.wait(), 2)
        task.cancel()
        await _settle()
        assert not task.done()
        assert wire.frame(wire.BOUNDARY, b"Q") not in s.writer.data
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert encoder.stopped and not mutations
        assert s.writer.data.count(wire.frame(wire.BOUNDARY, b"Q")) == 1
        assert not s.player.deferred
    finally:
        release.set()
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


@pytest.mark.parametrize("auto_play", [False, True])
async def test_double_previous_does_not_play_intro_before_capture_is_ready(auto_play):
    rig = Rig(limit=1000)
    recorded = []
    write = rig.writer.write

    def record(data):
        recorded.append(data)
        write(data)

    rig.writer.write = record
    original = rig.soloist.command

    async def command(name, **fields):
        result = await original(name, **fields)
        if auto_play and name == "skip_prev":
            rig.session.player.event({"type": "playback_changed", "status": "playing"})
        return result

    rig.soloist.command = command
    await rig.send(wire.AUDIO_REQUEST, b"1")
    try:
        # A periodic capture probe between the presses must not restart capture.
        rig.reader.feed_data(
            wire.frame(wire.CONTROL, b"b")
            + wire.frame(wire.AUDIO_REQUEST, b"1")
            + wire.frame(wire.CONTROL, b"b")
        )
        await _settle()
        expected = [("pause",), ("skip_prev",)] + ([("pause",)] if auto_play else [])
        assert rig.soloist.calls == expected  # final skip waits for capture to be ready
        assert len(rig.encoders) == 2
        assert rig.session.player.paused_by_us  # a started process alone is not capture readiness
        assert not any(c == ("play",) for c in rig.soloist.calls)
        await rig.encoders[-1].queue.put(b"warmup-silence")
        await _settle()
        assert rig.soloist.calls == expected + [("skip_prev",)] + ([] if auto_play else [("play",)])
        assert not rig.session.player.paused_by_us
        assert not rig.writer.frames  # the warmup silence is not sent into the retained ring
        await rig.packets(1)
        assert rig.writer.frames == [wire.audio(0, b"p0")]
        boundaries = [
            i for i, data in enumerate(recorded) if data == wire.frame(wire.BOUNDARY, b"b")
        ]
        assert len(boundaries) == 2
        assert recorded.index(wire.audio(0, b"p0")) > boundaries[-1]
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_failed_capture_priming_keeps_recovery_paused_and_can_retry(monkeypatch):
    monkeypatch.setattr(audio, "CAPTURE_READY_TIMEOUT", 0.03)
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    try:
        await rig.session.halt("stall")
        await rig.send(wire.AUDIO_REQUEST, b"1")
        await until(lambda: rig.encoders[-1].stopped)
        assert len(rig.encoders) == 2
        assert rig.soloist.calls == [("pause",)]
        assert rig.session.player.paused_by_us and not rig.session.streaming
        assert not rig.session.halting and not rig.session.tasks
        await rig.send(wire.AUDIO_REQUEST, b"1")
        await rig.encoders[-1].queue.put(b"warmup")
        await _settle()
        assert len(rig.encoders) == 3 and rig.soloist.calls[-1] == ("play",)
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_local_resume_waits_for_capture_before_playing():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    try:
        await rig.send(wire.CONTROL, b"s")
        await rig.send(wire.CONTROL, b"P")
        assert len(rig.encoders) == 2
        assert rig.soloist.calls == [("pause",)]
        await rig.encoders[-1].queue.put(b"warmup")
        await _settle()
        assert rig.soloist.calls == [("pause",), ("play",)]
        assert not rig.writer.frames
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_resume_trims_timestamped_paused_samples_with_bounded_decoder_history(monkeypatch):
    monkeypatch.setattr(audio.time, "time", lambda: 10.14)
    rig = Rig(limit=1000)
    frames = []
    write = rig.writer.write

    def record(data):
        frames.append(data)
        write(data)

    rig.writer.write = record
    await rig.send(wire.AUDIO_REQUEST, b"1")
    try:
        await rig.send(wire.CONTROL, b"s")
        await rig.send(wire.CONTROL, b"P")
        rig.encoders[-1].capture_at = 10.0
        await rig.packets(10)
        captures = [f[5:] for f in frames if f[:1] == wire.CAPTURE]
        assert len(captures) == 1
        assert int.from_bytes(captures[0][4:6]) == 5112  # 140 ms + delay minus 2 omitted packets
        assert len(captures[0]) == 6  # remaining warmup travels as zero-duration audio packets
        assert rig.writer.frames[0] == wire.audio(0, b"p2")
        assert len(rig.writer.frames) == 8
        assert rig.soloist.calls == [("pause",), ("play",)]
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_external_resume_starts_capture_without_waiting_for_periodic_probe():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    try:
        await rig.packets(40)  # old ACKs may still be in flight during a rapid resume
        await rig.send(wire.CONTROL, b"s")
        rig.session.player.event({"type": "playback_changed", "status": "playing"})
        await _settle()
        assert len(rig.encoders) == 2 and rig.session.streaming
        assert rig.soloist.calls == [("pause",)]  # the external Play is not duplicated
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


@pytest.mark.parametrize("external", [False, True])
async def test_pause_completion_follows_capture_tail_once(external):
    rig = Rig(limit=1000)
    recorded = []
    write = rig.writer.write

    def record(data):
        recorded.append(data)
        write(data)

    rig.writer.write = record
    await rig.send(wire.AUDIO_REQUEST, b"1")
    encoder = rig.encoders[0]
    stop = encoder.stop

    async def tail():
        await encoder.queue.put(b"pause-fade")
        await stop()

    encoder.stop = tail
    try:
        if external:
            rig.session.player.event({"type": "playback_changed", "status": "paused"})
        else:
            await rig.send(wire.CONTROL, b"s")
        await until(lambda: encoder.stopped)
        await _settle()
        boundary = wire.frame(wire.BOUNDARY, b"s")
        assert recorded.count(boundary) == 1
        assert recorded.index(wire.audio(0, b"pause-fade")) < recorded.index(boundary)
        assert not rig.session.streaming
    finally:
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)


async def test_disconnect_disarms_external_capture_work_before_control_drain():
    rig = Rig(limit=1000)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    draining, release = asyncio.Event(), asyncio.Event()
    stop = rig.encoders[0].stop

    async def slow_stop():
        draining.set()
        await release.wait()
        await stop()

    rig.encoders[0].stop = slow_stop
    try:
        await rig.send(wire.CONTROL, b"b")
        await asyncio.wait_for(draining.wait(), 2)
        rig.reader.feed_eof()
        await _settle()
        assert rig.session.player.capture_changed is None
        for active in (False, True):
            rig.session.player.event({"type": "device_changed", "is_active": active})
        release.set()
        await asyncio.wait([rig.task], timeout=2)
        assert rig.task.done() and isinstance(rig.task.exception(), asyncio.IncompleteReadError)
        assert len(rig.encoders) == 1
    finally:
        release.set()
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)
