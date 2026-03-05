"""
Module 8 — Market Manipulation Simulator

Generates synthetic orderbook data with injected manipulation patterns
for testing and demonstration.  Produces realistic-looking spoofing,
layering, whale walls, iceberg fills, and liquidity traps.
"""

from __future__ import annotations
import asyncio
import math
import random
import time
from typing import AsyncIterator

from backend.core import (
    OrderbookSnapshot, PriceLevel, TradeEvent, Side, EventBus,
)


class ManipulationSimulator:
    """
    Synthetic market data generator with configurable manipulation injection.
    """

    def __init__(
        self,
        event_bus: EventBus,
        symbol: str = "btcusdt",
        base_price: float = 65000.0,
        tick_interval: float = 0.1,
    ):
        self.bus = event_bus
        self.symbol = symbol
        self.base_price = base_price
        self.tick = tick_interval
        self._running = False
        self._seq = 0
        self._price = base_price
        self._volatility = 0.0002
        self._manipulation_queue: list[dict] = []

    # ── Controls ─────────────────────────────────────────────────────

    def schedule_manipulation(self, manip_type: str, delay: float = 0, **kwargs) -> None:
        """Schedule a manipulation event to be injected after `delay` seconds."""
        self._manipulation_queue.append({
            "type": manip_type,
            "trigger_time": time.time() + delay,
            "params": kwargs,
            "fired": False,
        })

    async def run(self, duration: float = 60.0) -> None:
        self._running = True
        start = time.time()
        while self._running and (time.time() - start) < duration:
            snap = self._generate_snapshot()
            await self.bus.publish("orderbook_snapshot", snap)
            await self.bus.publish("replay_record", snap)

            # Occasionally emit trades
            if random.random() < 0.3:
                trade = self._generate_trade()
                await self.bus.publish("trade_event", trade)

            await asyncio.sleep(self.tick)
        self._running = False

    def stop(self) -> None:
        self._running = False

    # ── Snapshot generation ──────────────────────────────────────────

    def _generate_snapshot(self) -> OrderbookSnapshot:
        now = time.time()
        self._seq += 1

        # Random walk
        self._price *= 1 + random.gauss(0, self._volatility)

        # Check for pending manipulations
        active_manips = [
            m for m in self._manipulation_queue
            if not m["fired"] and now >= m["trigger_time"]
        ]

        bids = self._build_levels(Side.BID, 20)
        asks = self._build_levels(Side.ASK, 20)

        # Inject manipulation patterns
        for m in active_manips:
            m["fired"] = True
            if m["type"] == "spoofing":
                bids, asks = self._inject_spoofing(bids, asks, m["params"])
            elif m["type"] == "layering":
                bids, asks = self._inject_layering(bids, asks, m["params"])
            elif m["type"] == "whale_wall":
                bids, asks = self._inject_whale_wall(bids, asks, m["params"])
            elif m["type"] == "liquidity_trap":
                bids, asks = self._inject_liquidity_trap(bids, asks, m["params"])

        return OrderbookSnapshot(
            symbol=self.symbol,
            timestamp=now,
            bids=bids,
            asks=asks,
            sequence=self._seq,
        )

    def _build_levels(self, side: Side, count: int) -> list[PriceLevel]:
        levels = []
        step = self._price * 0.0001  # 1 bps per level
        for i in range(count):
            offset = step * (i + 1)
            price = self._price - offset if side == Side.BID else self._price + offset
            qty = random.uniform(0.1, 2.0) * math.exp(-i * 0.1)
            levels.append(PriceLevel(price=round(price, 2), quantity=round(qty, 4)))
        return levels

    def _generate_trade(self) -> TradeEvent:
        side = random.choice([Side.BID, Side.ASK])
        slippage = random.gauss(0, self._price * 0.00005)
        return TradeEvent(
            symbol=self.symbol,
            price=round(self._price + slippage, 2),
            quantity=round(random.uniform(0.01, 0.5), 4),
            side=side,
            timestamp=time.time(),
        )

    # ── Manipulation injectors ───────────────────────────────────────

    def _inject_spoofing(
        self, bids: list[PriceLevel], asks: list[PriceLevel], params: dict
    ) -> tuple[list[PriceLevel], list[PriceLevel]]:
        """Place a massive wall that will be removed next tick."""
        side = params.get("side", "bid")
        size = params.get("size", 15.0)
        if side == "bid":
            bids.insert(0, PriceLevel(
                price=round(self._price - self._price * 0.0001, 2),
                quantity=size,
            ))
        else:
            asks.insert(0, PriceLevel(
                price=round(self._price + self._price * 0.0001, 2),
                quantity=size,
            ))
        return bids, asks

    def _inject_layering(
        self, bids: list[PriceLevel], asks: list[PriceLevel], params: dict
    ) -> tuple[list[PriceLevel], list[PriceLevel]]:
        """Place multiple large orders at consecutive levels."""
        side = params.get("side", "bid")
        layers = params.get("layers", 6)
        size = params.get("size", 8.0)
        target = bids if side == "bid" else asks
        step = self._price * 0.00015
        for i in range(layers):
            offset = step * (i + 1)
            price = self._price - offset if side == "bid" else self._price + offset
            target.insert(i, PriceLevel(
                price=round(price, 2),
                quantity=round(size * (1 - i * 0.05), 4),
            ))
        return bids, asks

    def _inject_whale_wall(
        self, bids: list[PriceLevel], asks: list[PriceLevel], params: dict
    ) -> tuple[list[PriceLevel], list[PriceLevel]]:
        """Single massive order dwarfing nearby liquidity."""
        side = params.get("side", "bid")
        size = params.get("size", 25.0)
        if side == "bid":
            bids[0] = PriceLevel(price=bids[0].price, quantity=size)
        else:
            asks[0] = PriceLevel(price=asks[0].price, quantity=size)
        return bids, asks

    def _inject_liquidity_trap(
        self, bids: list[PriceLevel], asks: list[PriceLevel], params: dict
    ) -> tuple[list[PriceLevel], list[PriceLevel]]:
        """Drain most liquidity from both sides."""
        drain = params.get("drain_factor", 0.05)
        bids = [PriceLevel(price=l.price, quantity=round(l.quantity * drain, 4)) for l in bids]
        asks = [PriceLevel(price=l.price, quantity=round(l.quantity * drain, 4)) for l in asks]
        # Widen spread
        if bids:
            bids[0] = PriceLevel(price=round(bids[0].price * 0.999, 2), quantity=bids[0].quantity)
        if asks:
            asks[0] = PriceLevel(price=round(asks[0].price * 1.001, 2), quantity=asks[0].quantity)
        return bids, asks


# ── Demo scenario builder ────────────────────────────────────────────

def build_demo_scenario(bus: EventBus, symbol: str = "btcusdt") -> ManipulationSimulator:
    """Pre-configured simulator with a sequence of manipulations for demo."""
    sim = ManipulationSimulator(bus, symbol=symbol, base_price=65000.0, tick_interval=0.08)

    # t+3s: spoofing on bid side
    sim.schedule_manipulation("spoofing", delay=3, side="bid", size=12.0)
    # t+5s: spoofing on ask side
    sim.schedule_manipulation("spoofing", delay=5, side="ask", size=15.0)
    # t+8s: layering on bid
    sim.schedule_manipulation("layering", delay=8, side="bid", layers=7, size=8.0)
    # t+12s: whale wall
    sim.schedule_manipulation("whale_wall", delay=12, side="ask", size=30.0)
    # t+18s: liquidity trap
    sim.schedule_manipulation("liquidity_trap", delay=18, drain_factor=0.03)
    # t+25s: another spoofing cycle
    sim.schedule_manipulation("spoofing", delay=25, side="bid", size=20.0)
    sim.schedule_manipulation("spoofing", delay=26, side="ask", size=18.0)
    # t+30s: layering + whale combo
    sim.schedule_manipulation("layering", delay=30, side="ask", layers=8, size=10.0)
    sim.schedule_manipulation("whale_wall", delay=31, side="bid", size=35.0)

    return sim
