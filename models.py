"""
Core data models and in-memory event bus for the OpenClaw pipeline.
All events flow through the EventBus as typed dataclass instances.
"""

from __future__ import annotations
import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional


# ── Enums ────────────────────────────────────────────────────────────

class Side(str, Enum):
    BID = "bid"
    ASK = "ask"


class AlertLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ManipulationType(str, Enum):
    SPOOFING = "spoofing"
    LAYERING = "layering"
    LIQUIDITY_TRAP = "liquidity_trap"
    WHALE_WALL = "whale_wall"
    ICEBERG = "iceberg"
    WASH_TRADE = "wash_trade"


# ── Data Models ──────────────────────────────────────────────────────

@dataclass(slots=True)
class PriceLevel:
    price: float
    quantity: float
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class OrderbookSnapshot:
    symbol: str
    timestamp: float
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    sequence: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    def bid_depth(self, levels: int = 5) -> float:
        return sum(l.quantity for l in self.bids[:levels])

    def ask_depth(self, levels: int = 5) -> float:
        return sum(l.quantity for l in self.asks[:levels])

    def imbalance_ratio(self, levels: int = 5) -> float:
        ad = self.ask_depth(levels)
        return self.bid_depth(levels) / ad if ad > 0 else float("inf")

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "mid_price": self.mid_price,
            "bids": [[l.price, l.quantity] for l in self.bids],
            "asks": [[l.price, l.quantity] for l in self.asks],
            "sequence": self.sequence,
        }


@dataclass(slots=True)
class TradeEvent:
    symbol: str
    price: float
    quantity: float
    side: Side
    timestamp: float
    trade_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class ManipulationAlert:
    alert_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    symbol: str = ""
    manipulation_type: ManipulationType = ManipulationType.SPOOFING
    level: AlertLevel = AlertLevel.LOW
    confidence: float = 0.0
    price_zone: tuple[float, float] = (0.0, 0.0)
    details: dict = field(default_factory=dict)
    related_snapshots: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "type": self.manipulation_type.value,
            "level": self.level.value,
            "confidence": round(self.confidence, 4),
            "price_zone": list(self.price_zone),
            "details": self.details,
        }


@dataclass
class ConfluenceSignal:
    timestamp: float = field(default_factory=time.time)
    symbol: str = ""
    score: float = 0.0
    components: list[ManipulationAlert] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "recommendation": self.recommendation,
            "components": [a.to_dict() for a in self.components],
        }


@dataclass
class HeatmapFrame:
    symbol: str
    timestamp: float
    price_min: float
    price_max: float
    grid: list[list[float]]  # [time_bin][price_bin] → intensity
    alerts_overlay: list[dict] = field(default_factory=list)


# ── Memory Queue ─────────────────────────────────────────────────────

class MemoryQueue:
    """Thread-safe bounded deque used as an in-memory ring buffer."""

    def __init__(self, maxlen: int = 10000):
        self._q: deque = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()

    async def push(self, item: Any) -> None:
        async with self._lock:
            self._q.append(item)

    async def pop(self) -> Any | None:
        async with self._lock:
            return self._q.popleft() if self._q else None

    async def peek_all(self) -> list:
        async with self._lock:
            return list(self._q)

    async def slice_time(self, start: float, end: float) -> list:
        async with self._lock:
            return [
                item for item in self._q
                if hasattr(item, "timestamp") and start <= item.timestamp <= end
            ]

    def __len__(self) -> int:
        return len(self._q)


# ── Event Bus ────────────────────────────────────────────────────────

Subscriber = Callable[..., Coroutine]


class EventBus:
    """Async pub/sub event bus. All pipeline stages communicate here."""

    def __init__(self):
        self._subscribers: dict[str, list[Subscriber]] = {}

    def subscribe(self, event_type: str, handler: Subscriber) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Subscriber) -> None:
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(handler)

    async def publish(self, event_type: str, data: Any) -> None:
        for handler in self._subscribers.get(event_type, []):
            try:
                await handler(data)
            except Exception as e:
                print(f"[EventBus] Error in handler for '{event_type}': {e}")

    async def publish_fire_and_forget(self, event_type: str, data: Any) -> None:
        for handler in self._subscribers.get(event_type, []):
            asyncio.create_task(handler(data))
