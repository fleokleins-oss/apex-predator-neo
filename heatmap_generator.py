"""
Module 3 — Manipulation Heatmap Generator

Builds 2D heatmaps (time × price) from orderbook depth and alert data.
Generates frames consumable by both canvas-based and WebGL renderers.

The heatmap encodes:
  - Base intensity: order depth at each (time, price) cell
  - Alert overlay: manipulation events highlighted with separate channel
  - Decay: older data fades naturally
"""

from __future__ import annotations
import math
import time
from collections import defaultdict

from backend.core import (
    OrderbookSnapshot, ManipulationAlert, HeatmapFrame, EventBus,
)
from config import settings


class HeatmapGenerator:
    """
    Accumulates orderbook snapshots into a rolling 2D grid and overlays
    manipulation alerts. Emits HeatmapFrame events for dashboard consumption.
    """

    def __init__(self, event_bus: EventBus, cfg: dict | None = None):
        self.bus = event_bus
        self.cfg = cfg or settings.HEATMAP
        self._price_bins = self.cfg["price_bins"]
        self._time_bins = self.cfg["time_bins"]

        # symbol → 2D grid [time_col][price_row]
        self._grids: dict[str, list[list[float]]] = {}
        # symbol → alert overlay grid
        self._alert_grids: dict[str, list[list[float]]] = {}
        # symbol → price range tracking
        self._price_range: dict[str, tuple[float, float]] = {}
        # symbol → time cursor (column index wraps around)
        self._cursor: dict[str, int] = defaultdict(int)
        # symbol → alert markers for JSON overlay
        self._alert_markers: dict[str, list[dict]] = defaultdict(list)

    # ── Public ───────────────────────────────────────────────────────

    async def on_snapshot(self, snap: OrderbookSnapshot) -> None:
        symbol = snap.symbol
        self._ensure_grid(symbol, snap)

        col = self._cursor[symbol] % self._time_bins
        grid = self._grids[symbol]
        p_min, p_max = self._price_range[symbol]

        # Clear current column
        grid[col] = [0.0] * self._price_bins

        # Fill from bids
        for lvl in snap.bids:
            row = self._price_to_row(lvl.price, p_min, p_max)
            if 0 <= row < self._price_bins:
                grid[col][row] += lvl.quantity

        # Fill from asks
        for lvl in snap.asks:
            row = self._price_to_row(lvl.price, p_min, p_max)
            if 0 <= row < self._price_bins:
                grid[col][row] += lvl.quantity

        # Normalize column
        col_max = max(grid[col]) if grid[col] else 1.0
        if col_max > 0:
            cap = self.cfg["intensity_cap"]
            grid[col] = [min(cap, v / col_max) for v in grid[col]]

        self._cursor[symbol] += 1

    async def on_alert(self, alert: ManipulationAlert) -> None:
        symbol = alert.symbol
        if symbol not in self._grids:
            return

        p_min, p_max = self._price_range[symbol]
        col = (self._cursor[symbol] - 1) % self._time_bins
        agrid = self._alert_grids[symbol]

        lo, hi = alert.price_zone
        row_lo = self._price_to_row(lo, p_min, p_max)
        row_hi = self._price_to_row(hi, p_min, p_max)
        for row in range(max(0, min(row_lo, row_hi)), min(self._price_bins, max(row_lo, row_hi) + 1)):
            agrid[col][row] = min(1.0, agrid[col][row] + alert.confidence)

        self._alert_markers[symbol].append({
            "col": col,
            "row_lo": row_lo,
            "row_hi": row_hi,
            "type": alert.manipulation_type.value,
            "confidence": alert.confidence,
            "timestamp": alert.timestamp,
        })
        # Keep only recent markers
        if len(self._alert_markers[symbol]) > 500:
            self._alert_markers[symbol] = self._alert_markers[symbol][-300:]

    async def get_frame(self, symbol: str) -> dict | None:
        if symbol not in self._grids:
            return None
        p_min, p_max = self._price_range[symbol]
        cursor = self._cursor[symbol]

        # Build ordered grid (oldest → newest)
        grid = self._grids[symbol]
        agrid = self._alert_grids[symbol]
        ordered = []
        a_ordered = []
        for i in range(self._time_bins):
            idx = (cursor + i) % self._time_bins
            ordered.append(grid[idx])
            a_ordered.append(agrid[idx])

        return {
            "type": "heatmap_frame",
            "symbol": symbol,
            "timestamp": time.time(),
            "price_min": p_min,
            "price_max": p_max,
            "price_bins": self._price_bins,
            "time_bins": self._time_bins,
            "depth_grid": ordered,
            "alert_grid": a_ordered,
            "markers": self._alert_markers.get(symbol, [])[-50:],
        }

    # ── Internal ─────────────────────────────────────────────────────

    def _ensure_grid(self, symbol: str, snap: OrderbookSnapshot) -> None:
        if symbol in self._grids:
            # Update price range if needed
            all_prices = [l.price for l in snap.bids + snap.asks if l.price > 0]
            if all_prices:
                new_min = min(all_prices)
                new_max = max(all_prices)
                margin = (new_max - new_min) * 0.1
                self._price_range[symbol] = (new_min - margin, new_max + margin)
            return

        all_prices = [l.price for l in snap.bids + snap.asks if l.price > 0]
        if not all_prices:
            return
        p_min, p_max = min(all_prices), max(all_prices)
        margin = (p_max - p_min) * 0.2
        self._price_range[symbol] = (p_min - margin, p_max + margin)
        self._grids[symbol] = [[0.0] * self._price_bins for _ in range(self._time_bins)]
        self._alert_grids[symbol] = [[0.0] * self._price_bins for _ in range(self._time_bins)]

    def _price_to_row(self, price: float, p_min: float, p_max: float) -> int:
        if p_max <= p_min:
            return 0
        ratio = (price - p_min) / (p_max - p_min)
        return int(ratio * (self._price_bins - 1))
