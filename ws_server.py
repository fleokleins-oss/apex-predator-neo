"""
Module 4 — WebSocket Server

Async WebSocket server that:
  - Wires together all pipeline stages
  - Broadcasts events to connected dashboard clients
  - Accepts control commands (replay, speed, seek)
  - Runs the simulator or connects to live Binance feed
"""

from __future__ import annotations
import asyncio
import json
import time
from typing import Set

try:
    import websockets
    from websockets.server import serve, WebSocketServerProtocol
except ImportError:
    websockets = None  # type: ignore

from backend.core import (
    EventBus, OrderbookSnapshot, ManipulationAlert, ConfluenceSignal,
    TradeEvent, Side, PriceLevel,
)
from backend.detectors import SpoofingDetector, WhaleDetector, LiquidityTrapDetector
from backend.engines import (
    ReplayEngine, HeatmapGenerator, ConfluenceEngine,
    ManipulationSimulator, build_demo_scenario,
)
from config import settings


class MarketGuardianServer:
    """
    Main server that orchestrates the full pipeline and broadcasts to
    WebSocket clients.
    """

    def __init__(self, host: str = "", port: int = 0):
        self.host = host or settings.WS_HOST
        self.port = port or settings.WS_PORT
        self.bus = EventBus()
        self.clients: Set = set()

        # Pipeline stages
        self.spoof_detector = SpoofingDetector(self.bus)
        self.whale_detector = WhaleDetector(self.bus)
        self.trap_detector = LiquidityTrapDetector(self.bus)
        self.confluence = ConfluenceEngine(self.bus)
        self.replay = ReplayEngine(self.bus)
        self.heatmap = HeatmapGenerator(self.bus)
        self.simulator: ManipulationSimulator | None = None

        self._setup_bus()
        self._heatmap_task: asyncio.Task | None = None

    def _setup_bus(self) -> None:
        # Snapshot → all detectors + heatmap + replay recorder
        self.bus.subscribe("orderbook_snapshot", self._on_snapshot)
        self.bus.subscribe("trade_event", self._on_trade)
        self.bus.subscribe("manipulation_alert", self._on_alert)
        self.bus.subscribe("confluence_signal", self._on_confluence)
        self.bus.subscribe("replay_frame", self._on_replay_frame)
        self.bus.subscribe("replay_record", self.replay.record_snapshot)

    # ── Event handlers (pipeline wiring) ─────────────────────────────

    async def _on_snapshot(self, snap: OrderbookSnapshot) -> None:
        await self.spoof_detector.on_snapshot(snap)
        await self.whale_detector.on_snapshot(snap)
        await self.trap_detector.on_snapshot(snap)
        await self.heatmap.on_snapshot(snap)
        await self._broadcast({
            "type": "orderbook",
            "data": snap.to_dict(),
        })

    async def _on_trade(self, trade: TradeEvent) -> None:
        await self.whale_detector.on_trade(trade)
        await self.trap_detector.on_trade(trade)
        await self._broadcast({
            "type": "trade",
            "data": {
                "symbol": trade.symbol,
                "price": trade.price,
                "quantity": trade.quantity,
                "side": trade.side.value,
                "timestamp": trade.timestamp,
            },
        })

    async def _on_alert(self, alert: ManipulationAlert) -> None:
        await self.confluence.on_alert(alert)
        await self.heatmap.on_alert(alert)
        await self.replay.record_alert(alert)
        await self._broadcast({
            "type": "alert",
            "data": alert.to_dict(),
        })

    async def _on_confluence(self, signal: ConfluenceSignal) -> None:
        await self._broadcast({
            "type": "confluence",
            "data": signal.to_dict(),
        })

    async def _on_replay_frame(self, frame: dict) -> None:
        await self._broadcast(frame)

    # ── Periodic heatmap push ────────────────────────────────────────

    async def _heatmap_loop(self) -> None:
        while True:
            for sym in settings.SYMBOLS:
                frame = await self.heatmap.get_frame(sym)
                if frame:
                    await self._broadcast(frame)
            await asyncio.sleep(1.0)

    # ── Client handling ──────────────────────────────────────────────

    async def _handle_client(self, ws: WebSocketServerProtocol) -> None:
        self.clients.add(ws)
        remote = ws.remote_address
        print(f"[WS] Client connected: {remote}")
        try:
            async for message in ws:
                await self._handle_command(ws, message)
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            print(f"[WS] Client disconnected: {remote}")

    async def _handle_command(self, ws, raw: str) -> None:
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError:
            return

        action = cmd.get("action")
        symbol = cmd.get("symbol", "btcusdt")

        if action == "start_demo":
            duration = cmd.get("duration", 60)
            await self._start_demo(symbol, duration)
            await ws.send(json.dumps({"type": "status", "message": "Demo started"}))

        elif action == "stop_demo":
            if self.simulator:
                self.simulator.stop()
            await ws.send(json.dumps({"type": "status", "message": "Demo stopped"}))

        elif action == "replay_start":
            speed = cmd.get("speed", 1.0)
            loop = cmd.get("loop", False)
            state = await self.replay.start_replay(symbol, speed, loop)
            await ws.send(json.dumps({"type": "replay_state", "data": self.replay.get_state(symbol)}))

        elif action == "replay_stop":
            await self.replay.stop_replay(symbol)
            await ws.send(json.dumps({"type": "replay_state", "data": self.replay.get_state(symbol)}))

        elif action == "replay_seek":
            frame = cmd.get("frame", 0)
            data = await self.replay.seek(symbol, frame)
            if data:
                await ws.send(json.dumps(data))

        elif action == "replay_speed":
            speed = cmd.get("speed", 1.0)
            await self.replay.set_speed(symbol, speed)

        elif action == "get_heatmap":
            frame = await self.heatmap.get_frame(symbol)
            if frame:
                await ws.send(json.dumps(frame))

        elif action == "get_state":
            await ws.send(json.dumps({
                "type": "full_state",
                "replay": self.replay.get_state(symbol),
                "confluence": self.confluence.get_latest(symbol),
            }))

    async def _start_demo(self, symbol: str, duration: float) -> None:
        if self.simulator:
            self.simulator.stop()
        self.simulator = build_demo_scenario(self.bus, symbol)
        asyncio.create_task(self.simulator.run(duration))

    # ── Broadcast ────────────────────────────────────────────────────

    async def _broadcast(self, data: dict) -> None:
        if not self.clients:
            return
        msg = json.dumps(data)
        dead = set()
        for ws in self.clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    # ── Server start ─────────────────────────────────────────────────

    async def start(self) -> None:
        if websockets is None:
            print("[ERROR] 'websockets' package not installed. pip install websockets")
            return

        self._heatmap_task = asyncio.create_task(self._heatmap_loop())
        print(f"[OpenClaw] Market Guardian server starting on ws://{self.host}:{self.port}")

        async with serve(self._handle_client, self.host, self.port):
            await asyncio.Future()  # run forever


# ── Entry point ──────────────────────────────────────────────────────

def main():
    server = MarketGuardianServer()
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
