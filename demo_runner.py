#!/usr/bin/env python3
"""
OpenClaw Predator — Demo Runner

Starts the full pipeline with the market manipulation simulator.
Open frontend/dashboard.html in a browser, then click "Start Demo".

Usage:
    cd openclaw
    python demo_runner.py
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.server.ws_server import MarketGuardianServer


async def main():
    print("=" * 60)
    print("  OpenClaw Predator + Binance AI Market Guardian")
    print("  Demo Mode")
    print("=" * 60)
    print()
    print("  WebSocket server: ws://localhost:8765")
    print("  Dashboard: open frontend/dashboard.html in browser")
    print("  Click '▶ Start Demo' in the dashboard to begin")
    print()
    print("  The simulator will inject manipulation patterns:")
    print("    t+3s   Spoofing (bid)")
    print("    t+5s   Spoofing (ask)")
    print("    t+8s   Layering (bid, 7 layers)")
    print("    t+12s  Whale wall (ask, 30 BTC)")
    print("    t+18s  Liquidity trap")
    print("    t+25s  Spoofing cycle")
    print("    t+30s  Layering + Whale combo")
    print()
    print("  Press Ctrl+C to stop")
    print("=" * 60)

    server = MarketGuardianServer(host="0.0.0.0", port=8765)
    await server.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[OpenClaw] Shutting down...")
