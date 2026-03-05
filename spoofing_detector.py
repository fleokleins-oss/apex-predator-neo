"""
Module 1 — Advanced Spoofing Detector

Detects spoofing, layering, and phantom liquidity by analyzing sequential
orderbook snapshots.  Maintains a rolling window of book states and looks for:

  1. Large orders placed then cancelled within a tight time window
  2. Layered orders at consecutive price levels (layering)
  3. Sudden bid/ask imbalance swings that revert (phantom walls)
  4. Repetitive placement-cancel cycles at the same price zone
"""

from __future__ import annotations
import time
from collections import defaultdict
from dataclasses import dataclass, field

from backend.core import (
    AlertLevel, ManipulationType, ManipulationAlert,
    OrderbookSnapshot, PriceLevel, Side, EventBus,
)
from config import settings


@dataclass(slots=True)
class _TrackedOrder:
    """Represents a large order being tracked for spoofing behaviour."""
    price: float
    quantity: float
    side: Side
    first_seen: float
    last_seen: float
    appearances: int = 1
    disappeared: bool = False
    disappear_time: float = 0.0


class SpoofingDetector:
    """
    Stateful detector that ingests OrderbookSnapshot streams and emits
    ManipulationAlert events via the EventBus.
    """

    def __init__(self, event_bus: EventBus, cfg: dict | None = None):
        self.bus = event_bus
        self.cfg = cfg or settings.SPOOFING

        # symbol → price → _TrackedOrder
        self._tracked: dict[str, dict[float, _TrackedOrder]] = defaultdict(dict)
        # symbol → list of confirmed spoofing events (for pattern counting)
        self._history: dict[str, list[ManipulationAlert]] = defaultdict(list)
        # rolling snapshots per symbol
        self._prev: dict[str, OrderbookSnapshot] = {}
        self._seq: int = 0

    # ── Public interface ─────────────────────────────────────────────

    async def on_snapshot(self, snap: OrderbookSnapshot) -> list[ManipulationAlert]:
        """Process a new orderbook snapshot. Returns any alerts generated."""
        alerts: list[ManipulationAlert] = []
        symbol = snap.symbol
        now = snap.timestamp

        # 1. Detect large orders appearing / disappearing
        alerts += self._track_walls(snap, now)

        # 2. Detect layering patterns
        alert = self._detect_layering(snap, now)
        if alert:
            alerts.append(alert)

        # 3. Detect imbalance reversal (phantom walls)
        alert = self._detect_phantom_wall(snap, now)
        if alert:
            alerts.append(alert)

        # Publish all alerts
        for a in alerts:
            await self.bus.publish("manipulation_alert", a)

        self._prev[symbol] = snap
        self._seq += 1
        return alerts

    # ── Wall tracking ────────────────────────────────────────────────

    def _track_walls(self, snap: OrderbookSnapshot, now: float) -> list[ManipulationAlert]:
        alerts = []
        symbol = snap.symbol
        tracked = self._tracked[symbol]
        min_size = self.cfg["min_wall_size_btc"]
        cancel_window = self.cfg["cancel_window_ms"] / 1000.0

        # Build current large-order set
        current_walls: dict[float, tuple[float, Side]] = {}
        for level in snap.bids:
            if level.quantity >= min_size:
                current_walls[level.price] = (level.quantity, Side.BID)
        for level in snap.asks:
            if level.quantity >= min_size:
                current_walls[level.price] = (level.quantity, Side.ASK)

        # Check previously tracked orders that vanished
        vanished_prices = set(tracked.keys()) - set(current_walls.keys())
        for price in vanished_prices:
            order = tracked[price]
            if not order.disappeared:
                order.disappeared = True
                order.disappear_time = now
                delta = now - order.last_seen
                if delta <= cancel_window:
                    confidence = min(1.0, order.appearances / self.cfg["repeat_threshold"])
                    alert = ManipulationAlert(
                        symbol=symbol,
                        manipulation_type=ManipulationType.SPOOFING,
                        level=self._confidence_to_level(confidence),
                        confidence=confidence,
                        price_zone=(price, price),
                        timestamp=now,
                        details={
                            "side": order.side.value,
                            "quantity": order.quantity,
                            "visible_ms": round(delta * 1000, 1),
                            "appearances": order.appearances,
                            "pattern": "place_cancel",
                        },
                    )
                    alerts.append(alert)
                    self._history[symbol].append(alert)

        # Update / add tracked orders
        for price, (qty, side) in current_walls.items():
            if price in tracked:
                t = tracked[price]
                t.last_seen = now
                t.quantity = qty
                t.appearances += 1
                t.disappeared = False
            else:
                tracked[price] = _TrackedOrder(
                    price=price, quantity=qty, side=side,
                    first_seen=now, last_seen=now,
                )

        # Prune stale entries (> 60s old disappeared)
        stale = [p for p, o in tracked.items() if o.disappeared and now - o.disappear_time > 60]
        for p in stale:
            del tracked[p]

        return alerts

    # ── Layering detection ───────────────────────────────────────────

    def _detect_layering(self, snap: OrderbookSnapshot, now: float) -> ManipulationAlert | None:
        depth = self.cfg["layering_depth"]
        min_size = self.cfg["min_wall_size_btc"]

        for side_label, levels in [("bid", snap.bids), ("ask", snap.asks)]:
            if len(levels) < depth:
                continue
            big_consecutive = 0
            start_price = levels[0].price
            for lvl in levels[:depth * 2]:
                if lvl.quantity >= min_size * 0.5:
                    big_consecutive += 1
                else:
                    big_consecutive = 0
                if big_consecutive >= depth:
                    end_price = lvl.price
                    total_qty = sum(l.quantity for l in levels[:depth * 2] if l.quantity >= min_size * 0.5)
                    confidence = min(1.0, big_consecutive / (depth + 2))
                    return ManipulationAlert(
                        symbol=snap.symbol,
                        manipulation_type=ManipulationType.LAYERING,
                        level=self._confidence_to_level(confidence),
                        confidence=confidence,
                        price_zone=(min(start_price, end_price), max(start_price, end_price)),
                        timestamp=now,
                        details={
                            "side": side_label,
                            "consecutive_levels": big_consecutive,
                            "total_quantity": round(total_qty, 4),
                            "pattern": "layered_walls",
                        },
                    )
        return None

    # ── Phantom wall / imbalance reversal ────────────────────────────

    def _detect_phantom_wall(self, snap: OrderbookSnapshot, now: float) -> ManipulationAlert | None:
        prev = self._prev.get(snap.symbol)
        if prev is None:
            return None

        ratio_now = snap.imbalance_ratio()
        ratio_prev = prev.imbalance_ratio()
        threshold = self.cfg["imbalance_ratio"]

        # Was heavily bid-biased, now not (or vice versa)
        if ratio_prev >= threshold and ratio_now < 1.0 / threshold:
            return ManipulationAlert(
                symbol=snap.symbol,
                manipulation_type=ManipulationType.SPOOFING,
                level=AlertLevel.HIGH,
                confidence=min(1.0, ratio_prev / (threshold * 2)),
                price_zone=(snap.best_bid, snap.best_ask),
                timestamp=now,
                details={
                    "pattern": "phantom_bid_wall",
                    "prev_imbalance": round(ratio_prev, 2),
                    "curr_imbalance": round(ratio_now, 2),
                },
            )

        if ratio_prev <= 1.0 / threshold and ratio_now > threshold:
            return ManipulationAlert(
                symbol=snap.symbol,
                manipulation_type=ManipulationType.SPOOFING,
                level=AlertLevel.HIGH,
                confidence=min(1.0, (1.0 / ratio_prev) / (threshold * 2)),
                price_zone=(snap.best_bid, snap.best_ask),
                timestamp=now,
                details={
                    "pattern": "phantom_ask_wall",
                    "prev_imbalance": round(ratio_prev, 2),
                    "curr_imbalance": round(ratio_now, 2),
                },
            )

        return None

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _confidence_to_level(c: float) -> AlertLevel:
        if c >= 0.85:
            return AlertLevel.CRITICAL
        if c >= 0.6:
            return AlertLevel.HIGH
        if c >= 0.35:
            return AlertLevel.MEDIUM
        return AlertLevel.LOW

    def get_history(self, symbol: str, limit: int = 50) -> list[dict]:
        return [a.to_dict() for a in self._history.get(symbol, [])[-limit:]]
