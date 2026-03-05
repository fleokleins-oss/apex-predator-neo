"""
Module 2 — Orderbook Replay Engine

Records orderbook snapshots into an in-memory ring buffer and replays them
at configurable speeds.  Supports:
  - Real-time recording from live feed
  - Time-range queries
  - Variable-speed replay (0.1× … 100×)
  - Snapshot-level seek
  - Event overlay (alerts are replayed in sync with book states)
"""

from __future__ import annotations
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

from backend.core import (
    OrderbookSnapshot, ManipulationAlert, EventBus, MemoryQueue,
)
from config import settings


@dataclass
class ReplayState:
    playing: bool = False
    speed: float = 1.0
    cursor: int = 0
    total_frames: int = 0
    current_timestamp: float = 0.0
    loop: bool = False


class ReplayEngine:
    """
    Captures and replays orderbook snapshots with synchronized alert overlays.
    """

    def __init__(self, event_bus: EventBus, max_frames: int = 0):
        self.bus = event_bus
        max_frames = max_frames or settings.QUEUE_MAX_SIZE
        # symbol → list of (snapshot, [alerts])
        self._buffer: dict[str, list[tuple[OrderbookSnapshot, list[ManipulationAlert]]]] = defaultdict(list)
        self._max_frames = max_frames
        self._pending_alerts: dict[str, list[ManipulationAlert]] = defaultdict(list)
        self._state: dict[str, ReplayState] = defaultdict(ReplayState)
        self._replay_tasks: dict[str, asyncio.Task] = {}

    # ── Recording ────────────────────────────────────────────────────

    async def record_snapshot(self, snap: OrderbookSnapshot) -> None:
        """Record a snapshot with any pending alerts attached."""
        symbol = snap.symbol
        alerts = self._pending_alerts.pop(symbol, [])
        buf = self._buffer[symbol]
        buf.append((snap, alerts))
        if len(buf) > self._max_frames:
            buf.pop(0)

    async def record_alert(self, alert: ManipulationAlert) -> None:
        """Buffer an alert to be attached to the next snapshot."""
        self._pending_alerts[alert.symbol].append(alert)

    # ── Replay Controls ──────────────────────────────────────────────

    async def start_replay(self, symbol: str, speed: float = 1.0, loop: bool = False) -> ReplayState:
        buf = self._buffer.get(symbol, [])
        if not buf:
            return ReplayState()

        if symbol in self._replay_tasks:
            self._replay_tasks[symbol].cancel()

        state = ReplayState(
            playing=True, speed=speed, cursor=0,
            total_frames=len(buf), loop=loop,
        )
        self._state[symbol] = state
        self._replay_tasks[symbol] = asyncio.create_task(self._replay_loop(symbol))
        return state

    async def stop_replay(self, symbol: str) -> ReplayState:
        if symbol in self._replay_tasks:
            self._replay_tasks[symbol].cancel()
            del self._replay_tasks[symbol]
        state = self._state.get(symbol, ReplayState())
        state.playing = False
        return state

    async def seek(self, symbol: str, frame_index: int) -> dict | None:
        buf = self._buffer.get(symbol, [])
        if not buf or frame_index >= len(buf):
            return None
        state = self._state[symbol]
        state.cursor = frame_index
        snap, alerts = buf[frame_index]
        state.current_timestamp = snap.timestamp
        return self._frame_to_dict(snap, alerts, frame_index, len(buf))

    async def set_speed(self, symbol: str, speed: float) -> None:
        self._state[symbol].speed = max(0.1, min(100.0, speed))

    # ── Query ────────────────────────────────────────────────────────

    async def get_time_range(
        self, symbol: str, start_ts: float, end_ts: float
    ) -> list[dict]:
        buf = self._buffer.get(symbol, [])
        results = []
        for i, (snap, alerts) in enumerate(buf):
            if start_ts <= snap.timestamp <= end_ts:
                results.append(self._frame_to_dict(snap, alerts, i, len(buf)))
        return results

    async def get_frame(self, symbol: str, index: int) -> dict | None:
        buf = self._buffer.get(symbol, [])
        if 0 <= index < len(buf):
            snap, alerts = buf[index]
            return self._frame_to_dict(snap, alerts, index, len(buf))
        return None

    def frame_count(self, symbol: str) -> int:
        return len(self._buffer.get(symbol, []))

    def get_state(self, symbol: str) -> dict:
        s = self._state.get(symbol, ReplayState())
        return {
            "playing": s.playing, "speed": s.speed, "cursor": s.cursor,
            "total_frames": s.total_frames, "current_timestamp": s.current_timestamp,
            "loop": s.loop,
        }

    # ── Internal replay loop ─────────────────────────────────────────

    async def _replay_loop(self, symbol: str) -> None:
        buf = self._buffer[symbol]
        state = self._state[symbol]

        try:
            while state.playing:
                if state.cursor >= len(buf):
                    if state.loop:
                        state.cursor = 0
                    else:
                        state.playing = False
                        break

                snap, alerts = buf[state.cursor]
                state.current_timestamp = snap.timestamp

                frame = self._frame_to_dict(snap, alerts, state.cursor, len(buf))
                await self.bus.publish("replay_frame", frame)

                # Calculate delay to next frame
                if state.cursor + 1 < len(buf):
                    next_snap = buf[state.cursor + 1][0]
                    delta = (next_snap.timestamp - snap.timestamp) / state.speed
                    await asyncio.sleep(max(0.001, delta))
                else:
                    await asyncio.sleep(0.01)

                state.cursor += 1

        except asyncio.CancelledError:
            pass
        finally:
            state.playing = False

    # ── Serialization ────────────────────────────────────────────────

    @staticmethod
    def _frame_to_dict(
        snap: OrderbookSnapshot, alerts: list[ManipulationAlert],
        index: int, total: int,
    ) -> dict:
        return {
            "type": "replay_frame",
            "frame_index": index,
            "total_frames": total,
            "snapshot": snap.to_dict(),
            "alerts": [a.to_dict() for a in alerts],
        }
