"""OpenClaw Predator + Binance AI Market Guardian — Configuration"""

BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"
BINANCE_REST_BASE = "https://api.binance.com/api/v3"
SYMBOLS = ["btcusdt", "ethusdt", "solusdt", "bnbusdt"]
DEPTH_LEVELS = 20

SPOOFING = {
    "min_wall_size_btc": 5.0,
    "cancel_window_ms": 800,
    "repeat_threshold": 3,
    "imbalance_ratio": 3.0,
    "layering_depth": 5,
    "confidence_decay": 0.95,
}

LIQUIDITY_TRAP = {
    "thin_book_threshold": 0.3,
    "spread_multiplier": 2.5,
    "volume_spike_ratio": 4.0,
    "trap_duration_ms": 5000,
}

WHALE = {
    "min_order_btc": 10.0,
    "iceberg_detection_window": 30,
    "accumulation_blocks": 10,
}

CONFLUENCE = {
    "min_signals": 2,
    "decay_seconds": 10,
    "weights": {"spoofing": 0.35, "liquidity_trap": 0.25, "whale": 0.40},
}

WS_HOST = "0.0.0.0"
WS_PORT = 8765

QUEUE_MAX_SIZE = 10000
HISTORY_RETENTION_SECONDS = 3600

HEATMAP = {
    "price_bins": 200,
    "time_bins": 600,
    "intensity_cap": 1.0,
}
