"""
Whale manipulation detector and liquidity trap detector.
"""

from __future__ import annotations
import time
from collections import defaultdict, deque

from backend.core import (
    AlertLevel, ManipulationType, ManipulationAlert,
    OrderbookSnapshot, TradeEvent, Side, EventBus,
)
from config import settings


# ═══════════════════════════════════════════════════════════════════════
#  Whale Detector
# ═══════════════════════════════════════════════════════════════════════

class WhaleDetector:
    """
    Detects whale walls, iceberg orders, and accumulation/distribution
    patterns from orderbook snapshots and trade streams.
    """

    def __init__(self, event_bus: EventBus, cfg: dict | None = None):
        self.bus = event_bus
        self.cfg = cfg or settings.WHALE
        self._trade_buffer: dict[str, deque[TradeEvent]] = defaultdict(
            lambda: deque(maxlen=2000)
        )
        self._wall_history: dict[str, deque[tuple[float, float, float]]] = defaultdict(
            lambda: deque(maxlen=500)
        )

    async def on_snapshot(self, snap: OrderbookSnapshot) -> list[ManipulationAlert]:
        alerts = []
        min_btc = self.cfg["min_order_btc"]
        now = snap.timestamp

        # Whale walls
        for side_label, levels in [("bid", snap.bids), ("ask", snap.asks)]:
            for lvl in levels[:10]:
                if lvl.quantity >= min_btc:
                    wall_pct = lvl.quantity / (snap.bid_depth(10) + snap.ask_depth(10))
                    if wall_pct > 0.15:
                        confidence = min(1.0, wall_pct * 3)
                        alerts.append(ManipulationAlert(
                            symbol=snap.symbol,
                            manipulation_type=ManipulationType.WHALE_WALL,
                            level=AlertLevel.HIGH if confidence > 0.6 else AlertLevel.MEDIUM,
                            confidence=confidence,
                            price_zone=(lvl.price, lvl.price),
                            timestamp=now,
                            details={
                                "side": side_label,
                                "quantity": lvl.quantity,
                                "depth_pct": round(wall_pct * 100, 1),
                                "pattern": "whale_wall",
                            },
                        ))

        # Iceberg detection: repeated same-size fills at same price
        alerts += self._detect_iceberg(snap.symbol, now)

        for a in alerts:
            await self.bus.publish("manipulation_alert", a)
        return alerts

    async def on_trade(self, trade: TradeEvent) -> None:
        self._trade_buffer[trade.symbol].append(trade)

    def _detect_iceberg(self, symbol: str, now: float) -> list[ManipulationAlert]:
        window = self.cfg["iceberg_detection_window"]
        trades = self._trade_buffer.get(symbol, deque())
        recent = [t for t in trades if now - t.timestamp <= window]
        if len(recent) < 5:
            return []

        # Group by price and check for repeated identical quantities
        price_groups: dict[float, list[float]] = defaultdict(list)
        for t in recent:
            price_groups[round(t.price, 2)].append(t.quantity)

        alerts = []
        for price, quantities in price_groups.items():
            if len(quantities) < 4:
                continue
            # Check if most quantities are identical (iceberg signature)
            from collections import Counter
            counts = Counter(round(q, 6) for q in quantities)
            top_qty, top_count = counts.most_common(1)[0]
            if top_count >= 4 and top_qty * top_count >= self.cfg["min_order_btc"]:
                confidence = min(1.0, top_count / 8)
                alerts.append(ManipulationAlert(
                    symbol=symbol,
                    manipulation_type=ManipulationType.ICEBERG,
                    level=AlertLevel.HIGH,
                    confidence=confidence,
                    price_zone=(price, price),
                    timestamp=now,
                    details={
                        "clip_size": top_qty,
                        "clip_count": top_count,
                        "total_filled": round(top_qty * top_count, 4),
                        "pattern": "iceberg_clips",
                    },
                ))
        return alerts


# ═══════════════════════════════════════════════════════════════════════
#  Liquidity Trap Detector
# ═══════════════════════════════════════════════════════════════════════

class LiquidityTrapDetector:
    """
    Detects liquidity traps: thin order books paired with volume spikes
    that exploit low-liquidity conditions for stop hunts or slippage attacks.
    """

    def __init__(self, event_bus: EventBus, cfg: dict | None = None):
        self.bus = event_bus
        self.cfg = cfg or settings.LIQUIDITY_TRAP
        self._depth_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._spread_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._volume_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._trap_start: dict[str, float | None] = defaultdict(lambda: None)

    async def on_snapshot(self, snap: OrderbookSnapshot) -> list[ManipulationAlert]:
        alerts = []
        symbol = snap.symbol
        now = snap.timestamp

        total_depth = snap.bid_depth(10) + snap.ask_depth(10)
        self._depth_history[symbol].append(total_depth)
        self._spread_history[symbol].append(snap.spread)

        depth_hist = self._depth_history[symbol]
        spread_hist = self._spread_history[symbol]

        if len(depth_hist) < 20:
            return alerts

        avg_depth = sum(depth_hist) / len(depth_hist)
        avg_spread = sum(spread_hist) / len(spread_hist)

        thin_book = total_depth < avg_depth * self.cfg["thin_book_threshold"]
        wide_spread = snap.spread > avg_spread * self.cfg["spread_multiplier"] if avg_spread > 0 else False

        if thin_book and wide_spread:
            if self._trap_start[symbol] is None:
                self._trap_start[symbol] = now
            elif (now - self._trap_start[symbol]) * 1000 >= self.cfg["trap_duration_ms"]:
                confidence = min(1.0, (avg_depth / max(total_depth, 0.001)) * 0.3)
                alerts.append(ManipulationAlert(
                    symbol=symbol,
                    manipulation_type=ManipulationType.LIQUIDITY_TRAP,
                    level=AlertLevel.CRITICAL if confidence > 0.7 else AlertLevel.HIGH,
                    confidence=confidence,
                    price_zone=(snap.best_bid, snap.best_ask),
                    timestamp=now,
                    details={
                        "depth_ratio": round(total_depth / max(avg_depth, 0.001), 3),
                        "spread_ratio": round(snap.spread / max(avg_spread, 0.001), 3),
                        "trap_duration_ms": round((now - self._trap_start[symbol]) * 1000),
                        "pattern": "liquidity_vacuum",
                    },
                ))
        else:
            self._trap_start[symbol] = None

        for a in alerts:
            await self.bus.publish("manipulation_alert", a)
        return alerts

    async def on_trade(self, trade: TradeEvent) -> None:
        self._volume_history[trade.symbol].append(trade.quantity)
