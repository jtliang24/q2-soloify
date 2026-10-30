"""Presentation timestamps and verified, context-preserving restoration of a pause bookmark.

The source can be a whole buffer ahead of the listener. Packet anchors use the capture's
sample timestamp, never TCP arrival time; occurrence tokens distinguish repeated tracks.
"""

import asyncio
from collections import deque
from copy import copy
from dataclasses import dataclass

import aiohttp

from .state import State


class RestoreError(Exception):
    """Keep the local output silent until another explicit Play retries the bookmark."""


@dataclass
class Occurrence:
    token: int
    state: State
    previous: "Occurrence | None" = None


class Timeline:
    def __init__(self):
        self.serial = 0
        self.current = None
        self.occurrences = deque(maxlen=128)
        self.history = deque(maxlen=4096)
        self.restoring = False

    def record(self, state, event, now):
        if self.restoring or state.state == "none" or not state.track:
            return
        if not state.anchor[1]:
            self.history.append((now, copy(state), 0))
            return
        previous = self.history[-1][1] if self.history else None
        changed = self.current is None or self.current.state.track.uri != state.track.uri
        # Repeat-track playback may change the position without changing the URI. A
        # backwards discontinuity creates a new occurrence, so it cannot seek the wrong lap.
        if previous and event.get("type") in ("position_sync", "playback_state"):
            changed |= (
                state.word == "playing"
                and previous.word == "playing"
                and state.position(now) + 1000 < previous.position(now)
            )
        if changed:
            self.serial += 1
            self.current = Occurrence(self.serial, copy(state), self.current)
            self.occurrences.append(self.current)
            self.occurrences[0].previous = None
        snapshot = copy(state)
        # Position timestamps describe the source clock. Track_changed has no timestamp;
        # its following position_sync supplies the track's actual starting time.
        at = state.anchor[1] if event.get("position") else now
        self.history.append((at, snapshot, self.current.token))

    def anchor(self, at):
        """State and occurrence for a presented sample, or None without a reliable anchor."""
        if at is None:
            return None
        # A late position_sync can describe a transition before track_changed arrived.
        # Prefer the newest source evidence covering this sample, rather than the
        # largest arrival timestamp, which would leave that transition mislabelled.
        for start, state, token in reversed(self.history):
            if start > at:
                continue
            if not token or state.word != "playing" or state.anchor[2] != 1:
                return None
            return state.lines(False, round(at)) + f"token={token}\n".encode()
        return None

    def known_start(self, state, now, cover=False):
        if (
            state.state != "none"
            and self.current
            and state.track
            and state.anchor[1]
            and self.current.state.track.uri == state.track.uri
        ):
            return state.lines(cover, now) + f"token={self.current.token}\n".encode()
        return state.lines(cover, now)


@dataclass
class Bookmark:
    correlation: bytes
    occurrence: Occurrence | None
    position: int
    route: list | None = None
    failed: str = ""
    source_uri: str = ""


class Rewind:
    TIMEOUT = 3
    TOTAL_TIMEOUT = 12

    def __init__(self, player):
        self.player = player
        self.bookmark = None
        self.navigating = False

    def remember(self, payload):
        if len(payload) < 4:
            raise RestoreError("Malformed pause bookmark")
        lines = dict(line.split("=", 1) for line in payload[4:].decode().splitlines())
        token = int(lines.get("token", 0))
        position = int(lines.get("position", 0))
        occurrence = next(
            (item for item in self.player.timeline.occurrences if item.token == token), None
        )
        if position < 0 or (
            occurrence
            and occurrence.state.track.duration
            and position > occurrence.state.track.duration
        ):
            occurrence = None
        if (
            self.bookmark
            and self.bookmark.occurrence is occurrence
            and self.bookmark.position == position
        ):
            self.bookmark.correlation = payload[:4]  # reconnect retains partial restore progress
            return
        self.bookmark = Bookmark(payload[:4], occurrence, position)

    async def command(self, name, **fields):
        if not self.player.ws or not await self.player.ws.command(name, **fields):
            raise RestoreError(f"Source refused {name}")

    async def snapshot(self, predicate):
        """A dispatch ACK is insufficient: query until actual source state confirms it."""
        async with asyncio.timeout(self.TIMEOUT):
            while True:
                event = await self.player.ws.request("get_state")
                state = State()
                state.apply(event)
                if not state.active:
                    raise RestoreError("Source is no longer active")
                if predicate(state):
                    return state
                await asyncio.sleep(0.02)

    @staticmethod
    def identity(entry):
        return entry.get("uid"), entry.get("item", {}).get("uri"), entry.get("source")

    async def queue(self):
        return await self.player.ws.request("get_queue", limit=128)

    async def navigate(self, op, before):
        """Confirm the occurrence actually visited, not just its URI or dispatch ACK.

        Soloist's history can include a manually queued item after a context item;
        Previous may pass that queued item. A verified Next path can then reach it.
        """
        entries = before.get("upcoming" if op == "skip_next" else "previous", [])
        if not entries:
            raise RestoreError("Queue navigation is unavailable")
        if op == "skip_next":
            entries = entries[:1]
        else:
            await self.command("seek", position_ms=0)
            await self.snapshot(lambda state: state.position() <= 100)
        await self.command(op)
        async with asyncio.timeout(self.TIMEOUT):
            while True:
                event = await self.player.ws.request("get_state")
                state = State()
                state.apply(event)
                after = await self.queue()
                remaining = after.get("upcoming" if op == "skip_next" else "previous", [])
                identities = {self.identity(entry) for entry in remaining}
                moved = [
                    entry
                    for entry in entries
                    if state.track
                    and entry.get("item", {}).get("uri") == state.track.uri
                    and self.identity(entry) not in identities
                ]
                if len(moved) == 1:
                    await self.command("pause")
                    await self.snapshot(
                        lambda s, uri=state.track.uri: (
                            s.word == "paused" and s.track and s.track.uri == uri
                        )
                    )
                    self.bookmark.source_uri = state.track.uri
                    return moved[0], after
                await asyncio.sleep(0.02)

    async def restore(self):
        try:
            async with asyncio.timeout(self.TOTAL_TIMEOUT):
                await self._restore()
        except TimeoutError as error:
            self.player.explicit_paused = True
            if self.bookmark:
                self.bookmark.failed = "Source restoration timed out"
            if self.player.ws:
                await self.player.ws.command("pause")
            raise RestoreError("Source restoration timed out") from error

    async def _restore(self):
        bookmark = self.bookmark
        if bookmark is None:
            return
        p = self.player
        p.explicit_paused = True
        p._stalled(False)
        p.timeline.restoring = True
        try:
            await self.command("pause")
            observed = await self.snapshot(lambda state: state.word == "paused")
            if bookmark.occurrence is None:
                raise RestoreError("No reliable audible bookmark; select a track or seek")
            target = bookmark.occurrence
            if (
                not observed.track
                or not p.timeline.current
                or observed.track.uri != (bookmark.source_uri or p.timeline.current.state.track.uri)
            ):
                raise RestoreError("Source track changed during pause")
            bookmark.source_uri = observed.track.uri
            if observed.context != target.state.context:
                raise RestoreError("Playback context changed")
            if bookmark.route is None:
                current = p.timeline.current
                route = []
                while current is not target:
                    if current is None or len(route) >= 128:
                        raise RestoreError("Track history expired")
                    current = current.previous
                    if current is not None:
                        route.append(current)
                current = p.timeline.current
                if any(item.state.context != target.state.context for item in [*route, current]):
                    raise RestoreError("Playback context changed")
                previous = (await self.queue()).get("previous", []) if route else []
                matches = [
                    entry
                    for entry in previous
                    if entry.get("item", {}).get("uri") == target.state.track.uri
                ]
                if route and not matches:
                    raise RestoreError("Previous track is unavailable")
                if len(matches) > 1:
                    # URI duplicates require an unambiguous sequence of occurrences
                    # and distinct UIDs. Do not assume the history's list direction.
                    candidates = []
                    for ordered in (previous, list(reversed(previous))):
                        if len(ordered) >= len(route) and all(
                            self.identity(entry)[1] == item.state.track.uri
                            for entry, item in zip(ordered, route, strict=False)
                        ):
                            candidates.append(ordered[len(route) - 1])
                    if (
                        len({self.identity(e) for e in matches}) != len(matches)
                        or len({self.identity(e) for e in candidates}) != 1
                    ):
                        raise RestoreError("Queue history orientation is ambiguous")
                    matches = candidates[:1]
                if matches and not matches[0].get("uid"):
                    raise RestoreError("Queue history is ambiguous or changed")
                bookmark.route = matches[:1] if route else []
            self.navigating = True
            steps = 0
            while bookmark.route:
                entry = bookmark.route[0]
                before = await self.queue()
                target_id = self.identity(entry)
                if target_id in {self.identity(e) for e in before.get("upcoming", [])}:
                    op = "skip_next"
                elif target_id in {self.identity(e) for e in before.get("previous", [])}:
                    op = "skip_prev"
                else:
                    raise RestoreError("Queue changed during rewind")
                steps += 1
                if steps > 128:
                    raise RestoreError("Queue navigation did not reach bookmark")
                moved, after = await self.navigate(op, before)
                if self.identity(moved) == target_id:
                    bookmark.route.clear()
                    p.timeline.current = target
                elif target_id not in {
                    self.identity(e) for e in after.get("previous", []) + after.get("upcoming", [])
                }:
                    raise RestoreError("Bookmarked queue entry disappeared")
            self.navigating = False
            await self.command("seek", position_ms=bookmark.position)
            await self.snapshot(
                lambda state: (
                    state.word == "paused"
                    and state.track
                    and state.track.uri == target.state.track.uri
                    and state.context == target.state.context
                    and state.shuffle == observed.shuffle
                    and state.repeat == observed.repeat
                    and abs(state.position() - bookmark.position) <= 100
                )
            )
            p.timeline.current = target
            p.timeline.history.clear()
            bookmark.failed = ""
        except (TimeoutError, ConnectionError, aiohttp.ClientError, RestoreError) as error:
            bookmark.failed = str(error) or type(error).__name__
            # Best effort source pause; the relay remains silent even if disconnected.
            if p.ws:
                await p.ws.command("pause")
            p.explicit_paused = True
            raise RestoreError(bookmark.failed) from error
        finally:
            self.navigating = False
            p.timeline.restoring = False
