"""Binance metadata — listing dates, trending/volume data, cached."""

import time
import logging
import requests

logger = logging.getLogger(__name__)

EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
TICKER_24H_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"

# Cache
_listing_cache: dict[str, int] = {}  # symbol -> onboardDate ms
_listing_cache_ts: float = 0

_ticker_cache: list[dict] = []
_ticker_cache_ts: float = 0

LISTING_CACHE_TTL = 3600    # 1 hour
TICKER_CACHE_TTL = 60       # 1 minute


def _refresh_listings():
    global _listing_cache, _listing_cache_ts
    if time.time() - _listing_cache_ts < LISTING_CACHE_TTL and _listing_cache:
        return
    try:
        r = requests.get(EXCHANGE_INFO_URL, timeout=10)
        if r.status_code == 200:
            symbols = r.json().get("symbols", [])
            _listing_cache = {
                s["symbol"]: s.get("onboardDate", 0)
                for s in symbols
                if s.get("status") == "TRADING"
            }
            _listing_cache_ts = time.time()
            logger.info(f"Refreshed listings: {len(_listing_cache)} symbols")
    except Exception as e:
        logger.error(f"Failed to fetch exchangeInfo: {e}")


def _refresh_tickers():
    global _ticker_cache, _ticker_cache_ts
    if time.time() - _ticker_cache_ts < TICKER_CACHE_TTL and _ticker_cache:
        return
    try:
        r = requests.get(TICKER_24H_URL, timeout=10)
        if r.status_code == 200:
            _ticker_cache = r.json()
            _ticker_cache_ts = time.time()
    except Exception as e:
        logger.error(f"Failed to fetch 24h tickers: {e}")


def get_listing_age_days(symbol: str) -> int | None:
    """Get how many days since the symbol was listed. None if unknown."""
    _refresh_listings()
    onboard = _listing_cache.get(symbol.upper())
    if not onboard:
        return None
    age_ms = int(time.time() * 1000) - onboard
    return max(0, age_ms // (86400 * 1000))


def is_recent_listing(symbol: str, max_days: int = 180) -> bool:
    """True if listed within max_days."""
    age = get_listing_age_days(symbol)
    if age is None:
        return False
    return age <= max_days


def get_volume_rank(symbol: str) -> dict | None:
    """Get 24h volume data and rank for a symbol.

    Returns {rank, total, quote_volume, price_change_pct, percentile}.
    """
    _refresh_tickers()
    if not _ticker_cache:
        return None

    # Sort by quoteVolume descending
    sorted_tickers = sorted(
        _ticker_cache,
        key=lambda t: float(t.get("quoteVolume", 0)),
        reverse=True,
    )

    total = len(sorted_tickers)
    for i, t in enumerate(sorted_tickers):
        if t["symbol"].upper() == symbol.upper():
            rank = i + 1
            return {
                "rank": rank,
                "total": total,
                "quote_volume": float(t.get("quoteVolume", 0)),
                "price_change_pct": float(t.get("priceChangePercent", 0)),
                "percentile": round((1 - rank / total) * 100, 1),
            }
    return None


def is_trending(symbol: str, top_pct: int = 20) -> bool:
    """True if symbol is in the top N% by 24h volume."""
    info = get_volume_rank(symbol)
    if not info:
        return False
    return info["percentile"] >= (100 - top_pct)
