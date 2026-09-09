from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "aegis_alpha_mvp.db"
FEED_CONFIG_PATH = BASE_DIR / "aegis_feeds.json"
OUTBOX_DIR = BASE_DIR / "outbox"
WECOM_CONFIG_PATH = BASE_DIR / "aegis_wecom.json"
MODEL_NAME = os.getenv("AEGIS_MODEL_NAME", "deepseek-chat")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")
