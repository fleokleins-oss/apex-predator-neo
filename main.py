"""
main.py
APEX PREDATOR NEO v666.1 – Entry Point

Roles (via APEX_ROLE env var):
  scanner     -> LOB + fee init + triangle scan
  executor    -> Atomic execution engine
  lob_service -> Dedicated WebSocket LOB ingestion
"""
from __future__ import annotations

import asyncio
import signal
import sys

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from loguru import logger
from config.config import cfg


def setup_logging() -> None:
    logger.remove()
    fmt = (
        "<green>{time:HH:mm:ss.SSS}</green> | "
        "<level>{level:<8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )
    logger.add(sys.stderr, level=cfg.LOG_LEVEL, format=fmt, colorize=True)
    logger.add(
        f"/app/logs/apex_{cfg.APEX_ROLE}_{cfg.APEX_REGION}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} | {message}",
        rotation=cfg.LOG_ROTATION,
        retention=cfg.LOG_RETENTION,
        compression="gz",
        enqueue=True,
    )
    logger.add(
        "/app/logs/apex_errors.log",
        level="ERROR",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        enqueue=True,
    )


# ═══════════════════════════════════════════════════════
# LOB SERVICE MODE
# ═══════════════════════════════════════════════════════
async def run_lob_service() -> None:
    """Dedicated LOB ingestion — runs WebSocket streams only."""
    from core.binance_connector import connector
    from core.lob_manager import lob
    from utils.redis_pubsub import redis_bus

    logger.info("=" * 58)
    logger.info("  📚 APEX v666.1 — LOB SERVICE MODE")
    logger.info("=" * 58)

    await redis_bus.connect()
    await connector.connect()

    symbols = connector.symbols
    lob.subscribe(symbols)
    await lob.start()

    logger.success(f"LOB service running: {len(symbols)} symbols streaming")

    try:
        while True:
            await asyncio.sleep(60)
            stale = sum(
                1 for s in lob._subscribed_symbols
                if (b := lob._books.get(s)) and b.is_stale
            )
            total = len(lob._subscribed_symbols)
            logger.info(f"📚 LOB health: {total - stale}/{total} fresh")
    except asyncio.CancelledError:
        pass
    finally:
        await lob.stop()
        await connector.disconnect()
        await redis_bus.disconnect()


# ═══════════════════════════════════════════════════════
# SCANNER MODE
# ═══════════════════════════════════════════════════════
async def run_scanner() -> None:
    from core.binance_connector import connector
    from core.fee_manager import fee_manager
    from core.robin_hood_risk import robin_hood
    from scanners.dynamic_tri_scanner import scanner
    from utils.redis_pubsub import redis_bus

    logger.info("=" * 58)
    logger.info("  🦈 APEX v666.1 — SCANNER (LOB + Dynamic Fees)")
    logger.info(f"  Testnet: {cfg.TESTNET} | Capital: ${cfg.CAPITAL_TOTAL:.2f}")
    logger.info(f"  Scan: {cfg.SCAN_INTERVAL_MS}ms | Region: {cfg.APEX_REGION}")
    logger.info("=" * 58)

    await redis_bus.connect()
    await connector.connect()

    await fee_manager.initialize()
    maker, taker = fee_manager.get_fees()
    logger.info(
        f"💰 Fees: maker={maker:.5f} taker={taker:.5f} "
        f"BNB={fee_manager.bnb_discount_active}"
    )

    bal = await connector.get_balance("USDT")
    await robin_hood.initialize(bal)
    logger.info(f"💰 USDT balance: ${bal:.4f}")

    count = await scanner.discover()
    if count == 0:
        logger.error("❌ No triangles found — check pairs and testnet/live mode")
        await connector.disconnect()
        await redis_bus.disconnect()
        return

    try:
        await scanner.run()
    except asyncio.CancelledError:
        pass
    finally:
        scanner.stop()
        from core.lob_manager import lob
        await lob.stop()
        await connector.disconnect()
        await redis_bus.disconnect()


# ═══════════════════════════════════════════════════════
# EXECUTOR MODE
# ═══════════════════════════════════════════════════════
async def run_executor() -> None:
    from core.binance_connector import connector
    from core.fee_manager import fee_manager
    from core.robin_hood_risk import robin_hood
    from utils.redis_pubsub import redis_bus

    if cfg.APEX_REGION == "singapore":
        from executors.singapore_executor import SingaporeExecutor
        executor = SingaporeExecutor()
    elif cfg.APEX_REGION == "tokyo":
        from executors.tokyo_executor import TokyoExecutor
        executor = TokyoExecutor()
    else:
        logger.error(f"Unknown region: {cfg.APEX_REGION}")
        return

    logger.info("=" * 58)
    logger.info(f"  🦈 APEX v666.1 — EXECUTOR [{cfg.APEX_REGION.upper()}] (Atomic)")
    logger.info(f"  Testnet: {cfg.TESTNET}")
    logger.info("=" * 58)

    await redis_bus.connect()
    await connector.connect()
    await fee_manager.initialize()

    bal = await connector.get_balance("USDT")
    await robin_hood.initialize(bal)

    await executor.start()

    try:
        await redis_bus.listen()
    except asyncio.CancelledError:
        pass
    finally:
        await connector.disconnect()
        await redis_bus.disconnect()


# ═══════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════
def main() -> None:
    setup_logging()
    logger.info(
        f"🦈 APEX PREDATOR NEO v666.1 | "
        f"Role: {cfg.APEX_ROLE} | Region: {cfg.APEX_REGION} | "
        f"Testnet: {cfg.TESTNET}"
    )

    if cfg.APEX_ROLE not in ("lob_service",) and (
        not cfg.api_key or not cfg.api_secret
    ):
        logger.critical("❌ API keys not configured! Fill .env file.")
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def on_signal(sig, _frame):
        logger.warning(f"⚠️ Signal {sig} — graceful shutdown...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    runners = {
        "scanner": run_scanner,
        "executor": run_executor,
        "lob_service": run_lob_service,
    }

    runner = runners.get(cfg.APEX_ROLE)
    if not runner:
        logger.error(f"Invalid APEX_ROLE: {cfg.APEX_ROLE}")
        sys.exit(1)

    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:
        pass
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        logger.info("🏁 APEX v666.1 shutdown complete")


if __name__ == "__main__":
    main()
