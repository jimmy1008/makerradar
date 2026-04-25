"""Configuration — loads .env and provides typed access."""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")


class Config:
    port: int = int(os.getenv("PORT", "8111"))

    # Telegram listener (telethon)
    tg_api_id: int = int(os.getenv("TG_API_ID", "0"))
    tg_api_hash: str = os.getenv("TG_API_HASH", "")
    tg_channels: list[str] = [
        ch.strip() for ch in os.getenv("TG_CHANNELS", "").split(",") if ch.strip()
    ]

    # Telegram bot (signal push)
    tg_bot_token: str = os.getenv("TG_BOT_TOKEN", "")
    tg_chat_id: str = os.getenv("TG_CHAT_ID", "")

    # Claude API (for Square analysis)
    claude_api_key: str = os.getenv("CLAUDE_API_KEY", "")
