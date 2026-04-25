"""Social & sentiment data — CoinGecko trending, community, volume rank changes."""

import time
import logging
import requests

import db

logger = logging.getLogger(__name__)

GECKO_BASE = "https://api.coingecko.com/api/v3"

# ── Cache ──
_trending_cache: dict[str, int] = {}  # symbol -> rank (1-based)
_trending_ts: float = 0
_TRENDING_TTL = 300  # 5 min

_vol_rank_history: dict[str, list[tuple[float, int]]] = {}  # symbol -> [(ts, rank)]


def _refresh_trending():
    global _trending_cache, _trending_ts
    if time.time() - _trending_ts < _TRENDING_TTL and _trending_cache:
        return
    try:
        r = requests.get(f"{GECKO_BASE}/search/trending", timeout=10)
        if r.status_code == 200:
            coins = r.json().get("coins", [])
            _trending_cache = {
                c["item"]["symbol"].upper(): i + 1
                for i, c in enumerate(coins)
            }
            _trending_ts = time.time()
            logger.info(f"Trending refreshed: {len(_trending_cache)} coins")
    except Exception as e:
        logger.error(f"Trending fetch error: {e}")


def is_trending_gecko(symbol: str) -> dict | None:
    """Check if symbol is in CoinGecko trending. Returns {rank} or None."""
    _refresh_trending()
    sym = symbol.upper().replace("USDT", "")
    rank = _trending_cache.get(sym)
    if rank:
        return {"rank": rank, "total": len(_trending_cache)}
    return None


def get_community_data(symbol: str) -> dict | None:
    """Fetch community/sentiment data from CoinGecko.

    Returns {telegram_users, sentiment_up, reddit_active} or None.
    Rate limited — use sparingly.
    """
    sym = symbol.upper().replace("USDT", "")
    try:
        # Search for coin ID first
        r = requests.get(f"{GECKO_BASE}/search", params={"query": sym}, timeout=10)
        if r.status_code != 200:
            return None
        coins = r.json().get("coins", [])
        # Find exact match
        coin_id = None
        for c in coins:
            if c["symbol"].upper() == sym:
                coin_id = c["id"]
                break
        if not coin_id:
            return None

        # Fetch community data
        r2 = requests.get(f"{GECKO_BASE}/coins/{coin_id}", params={
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "true",
            "developer_data": "false",
        }, timeout=10)
        if r2.status_code != 200:
            return None

        data = r2.json()
        cd = data.get("community_data", {})
        return {
            "telegram_users": cd.get("telegram_channel_user_count") or 0,
            "reddit_active": cd.get("reddit_accounts_active_48h") or 0,
            "reddit_posts_48h": cd.get("reddit_average_posts_48h") or 0,
            "sentiment_up": data.get("sentiment_votes_up_percentage"),
        }
    except Exception as e:
        logger.error(f"Community data {sym}: {e}")
        return None


def track_volume_rank_change(symbol: str, current_rank: int) -> int | None:
    """Track volume rank over time and return rank change (negative = improving).

    Stores last 24h of rank snapshots in memory.
    Returns delta (e.g., -50 means jumped up 50 positions).
    """
    now = time.time()
    key = symbol.upper()

    if key not in _vol_rank_history:
        _vol_rank_history[key] = []

    _vol_rank_history[key].append((now, current_rank))

    # Keep only last 24h
    cutoff = now - 86400
    _vol_rank_history[key] = [
        (ts, r) for ts, r in _vol_rank_history[key] if ts > cutoff
    ]

    history = _vol_rank_history[key]
    if len(history) < 2:
        return None

    # Compare current rank with earliest in window
    oldest_rank = history[0][1]
    delta = current_rank - oldest_rank  # negative = rank improved
    return delta


def get_social_score(symbol: str, vol_rank: dict | None = None) -> dict:
    """Aggregate social/sentiment signals for a symbol.

    Returns {
        trending: bool,
        trending_rank: int|None,
        vol_rank_delta: int|None,
        community: dict|None,
        social_score: int (0-5),
        social_tags: list[str],
    }
    """
    tags = []
    score = 0

    # CoinGecko trending
    trending = is_trending_gecko(symbol)
    if trending:
        tags.append(f"CG熱搜#{trending['rank']}")
        score += 2

    # Volume rank change
    vol_delta = None
    if vol_rank and vol_rank.get("rank"):
        vol_delta = track_volume_rank_change(symbol, vol_rank["rank"])
        if vol_delta and vol_delta < -30:
            tags.append(f"量排飆升{abs(vol_delta)}位")
            score += 2
        elif vol_delta and vol_delta < -10:
            tags.append(f"量排上升{abs(vol_delta)}位")
            score += 1

    # Community (rate limited, only fetch for promising signals)
    community = None  # Fetched on demand to avoid rate limits

    return {
        "trending": trending is not None,
        "trending_rank": trending["rank"] if trending else None,
        "vol_rank_delta": vol_delta,
        "community": community,
        "social_score": min(5, score),
        "social_tags": tags,
    }
