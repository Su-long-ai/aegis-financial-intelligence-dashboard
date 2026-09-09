"""Backward-compatible CLI entrypoint for the packaged Aegis implementation."""

from aegis.cli import build_cli, main, parse_price_updates
from aegis.config import DB_PATH, FEED_CONFIG_PATH, OUTBOX_DIR, WECOM_CONFIG_PATH
from aegis.domain import AssetImpact, EventClassification
from aegis.notifications import WeComConfig, WeComNotifier
from aegis.pipeline import AegisPipeline

__all__ = [
    "AegisPipeline", "AssetImpact", "EventClassification", "WeComConfig", "WeComNotifier",
    "DB_PATH", "FEED_CONFIG_PATH", "OUTBOX_DIR", "WECOM_CONFIG_PATH", "build_cli", "parse_price_updates", "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
