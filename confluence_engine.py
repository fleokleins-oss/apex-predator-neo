"""
Confluence Engine — Aggregates signals from multiple detectors and
produces weighted composite scores with actionable recommendations.
"""

from __future__ import annotations
import time
from collections import defaultdict, deque

from backend.core import (
    ManipulationAlert, ConfluenceSignal, EventBus, ManipulationType,
)
from config import settings


class ConfluenceEngine:
    """
    Receives ManipulationAlerts from all detectors and produces
    ConfluenceSignals when multiple concurrent signals align.
    """

    def __init__(self, event_bus: EventBus, cfg: dict | None = None):
        self.bus = event_bus
        self.cfg = cfg or settings.CONFLUENCE
        self._active: dict[str, deque[ManipulationAlert]] = defaultdict(
            lambda: deque(maxlen=200)
        )
        self._last_signal: dict[str, ConfluenceSignal] = {}

    async def on_alert(self, alert: ManipulationAlert) -> ConfluenceSignal | None:
        symbol = alert.symbol
        now = time.time()
        self._active[symbol].append(alert)

        # Prune expired
        decay = self.cfg["decay_seconds"]
        active = [a for a in self._active[symbol] if now - a.timestamp <= decay]
        self._active[symbol] = deque(active, maxlen=200)

        if len(active) < self.cfg["min_signals"]:
            return None

        # Weighted score
        weights = self.cfg["weights"]
        type_map = {
            ManipulationType.SPOOFING: "spoofing",
            ManipulationType.LAYERING: "spoofing",
            ManipulationType.LIQUIDITY_TRAP: "liquidity_trap",
            ManipulationType.WHALE_WALL: "whale",
            ManipulationType.ICEBERG: "whale",
            ManipulationType.WASH_TRADE: "whale",
        }

        score = 0.0
        seen_types = set()
        for a in active:
            key = type_map.get(a.manipulation_type, "spoofing")
            w = weights.get(key, 0.2)
            score += a.confidence * w
            seen_types.add(key)

        score = min(1.0, score / max(len(seen_types), 1))

        recommendation = self._generate_recommendation(score, active)

        signal = ConfluenceSignal(
            symbol=symbol,
            score=score,
            components=list(active),
            recommendation=recommendation,
            timestamp=now,
        )
        self._last_signal[symbol] = signal
        await self.bus.publish("confluence_signal", signal)
        return signal

    def get_latest(self, symbol: str) -> dict | None:
        sig = self._last_signal.get(symbol)
        return sig.to_dict() if sig else None

    @staticmethod
    def _generate_recommendation(score: float, alerts: list[ManipulationAlert]) -> str:
        types = {a.manipulation_type for a in alerts}
        if score >= 0.8:
            return "CRITICAL: Multiple manipulation vectors detected. Avoid limit orders in affected zone. Consider pausing automated strategies."
        if score >= 0.5:
            if ManipulationType.LIQUIDITY_TRAP in types:
                return "WARNING: Liquidity trap conditions with corroborating signals. Widen slippage tolerance or delay execution."
            return "WARNING: Elevated manipulation probability. Use caution with large orders."
        if score >= 0.3:
            return "WATCH: Moderate manipulation signals. Monitor for escalation."
        return "INFO: Low-level signals detected. Normal market noise likely."
