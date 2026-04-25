"""Active scanner — proactively scans all small-cap Binance futures for MM activity.

Runs every 5 minutes. Doesn't wait for TG channels.
Detects:
1. OI creeping up while price is flat (stealth accumulation)
2. Volume spike with flat price (absorption)
3. Cross-exchange OI buildup (Binance + Hyperliquid)
"""

import time
import logging
import requests
from datetime import datetime, timezone, timedelta

import db
from indicators import fetch_hype_data, fetch_smart_money, analyze, calc_tp_sl
from binance_meta import get_listing_age_days, get_volume_rank
from social import get_social_score
from tg_bot import send_signal
from tg_parser import Alert

logger = logging.getLogger(__name__)
TW = timezone(timedelta(hours=8))

BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# In-memory OI snapshots for delta detection
_oi_snapshots: dict[str, list[tuple[float, float]]] = {}  # symbol -> [(timestamp, oi_usd)]

MAX_MCAP = 200_000_000
SCAN_INTERVAL = 300  # 5 min


def _get_small_cap_symbols() -> list[dict]:
    """Get all USDT futures symbols with MCap < $200M."""
    try:
        r = requests.get(BINANCE_EXCHANGE_INFO, timeout=10)
        if r.status_code != 200:
            return []
        symbols = r.json().get("symbols", [])
        from signal_filter import EXCLUDED_SYMBOLS
        return [
            s for s in symbols
            if s["status"] == "TRADING"
            and s["symbol"].endswith("USDT")
            and s["baseAsset"] not in EXCLUDED_SYMBOLS
        ]
    except Exception as e:
        logger.error(f"Exchange info: {e}")
        return []


def _get_all_oi() -> dict[str, float]:
    """Batch fetch OI for all futures symbols. Returns {symbol: oi_value}."""
    try:
        # Use 24h ticker which includes everything
        r = requests.get(BINANCE_TICKER_URL, timeout=10)
        if r.status_code != 200:
            return {}
        tickers = r.json()
        result = {}
        for t in tickers:
            sym = t["symbol"]
            # OI not in ticker, need separate call per symbol — too slow
            # Instead use the batch endpoint
            pass

        # Use openInterest endpoint per symbol is too slow for 700+
        # Alternative: use /fapi/v1/ticker/24hr for price data + separate OI scan for shortlist
        return {}
    except Exception as e:
        logger.error(f"Batch OI: {e}")
        return {}


def _get_oi(symbol: str) -> float | None:
    """Get current OI for a single symbol."""
    try:
        r = requests.get(BINANCE_OI_URL, params={"symbol": symbol}, timeout=5)
        if r.status_code == 200:
            return float(r.json().get("openInterest", 0))
    except Exception:
        pass
    return None


def _get_price(symbol: str) -> float | None:
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price",
                         params={"symbol": symbol}, timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    return None


def _record_oi(symbol: str, oi_usd: float):
    """Store OI snapshot for delta tracking."""
    now = time.time()
    if symbol not in _oi_snapshots:
        _oi_snapshots[symbol] = []
    _oi_snapshots[symbol].append((now, oi_usd))
    # Keep last 48h
    cutoff = now - 48 * 3600
    _oi_snapshots[symbol] = [(t, o) for t, o in _oi_snapshots[symbol] if t > cutoff]


def _get_oi_delta(symbol: str, hours: int = 1) -> dict | None:
    """Get OI change over last N hours."""
    history = _oi_snapshots.get(symbol, [])
    if len(history) < 2:
        return None
    now = time.time()
    cutoff = now - hours * 3600
    old = [o for t, o in history if t <= cutoff + 300]  # Allow 5min slack
    if not old:
        old_val = history[0][1]
    else:
        old_val = old[-1]
    new_val = history[-1][1]
    if old_val == 0:
        return None
    pct = (new_val - old_val) / old_val * 100
    return {"old": old_val, "new": new_val, "delta_pct": round(pct, 2), "delta_usd": new_val - old_val}


def _build_scanner_signal(symbol: str, reasons: list, score: int, ta: dict, alert_data: dict, warnings: list = None) -> dict | None:
    """Build a signal dict compatible with the existing signal pipeline."""
    from signal_filter import _calc_chase_threshold

    confidence = "high" if score >= 8 else "mid" if score >= 4 else "low"
    level = "strong" if len(reasons) >= 4 else "medium" if len(reasons) >= 2 else "weak"

    entry_price = ta.get("current_price")
    atr = ta.get("atr")
    tp_sl = {}
    if entry_price and atr:
        tp_sl = calc_tp_sl(entry_price, atr, confidence)

    return {
        "symbol": symbol.replace("USDT", ""),
        "pair": symbol,
        "level": level,
        "signal_type": "scanner",
        "confidence": confidence,
        "score": score,
        "chase_threshold": 0,
        "hit_count": 0,
        "reasons": reasons,
        "warnings": warnings or [],
        "alert": alert_data,
        "ta": ta,
        "listing_age": get_listing_age_days(symbol),
        "vol_rank": get_volume_rank(symbol),
        "social": get_social_score(symbol.replace("USDT", ""), get_volume_rank(symbol)),
        "oi_accum": None,
        "tp_sl": tp_sl,
    }


def scan_losers_board():
    """Scan 24h biggest losers — beaten down coins with bounce potential.

    Strategy from 拉哪: pick coins from the losers board, watch for reversal.
    Auto-adds to watchlist for 4H reversal detection.
    """
    from signal_filter import EXCLUDED_SYMBOLS
    from watchlist_scanner import add_to_watchlist

    try:
        r = requests.get(BINANCE_TICKER_URL, timeout=10)
        if r.status_code != 200:
            return
        tickers = r.json()

        # Filter USDT perpetuals, sort by price change ascending (biggest losers)
        losers = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            base = sym.replace("USDT", "")
            if base in EXCLUDED_SYMBOLS:
                continue
            change = float(t.get("priceChangePercent", 0))
            vol = float(t.get("quoteVolume", 0))
            # Significant losers with decent volume
            if change <= -10 and vol > 1_000_000:
                losers.append({
                    "symbol": sym, "base": base,
                    "change": change, "volume": vol,
                })

        losers.sort(key=lambda x: x["change"])
        added = 0
        for loser in losers[:15]:  # Top 15 biggest losers
            if add_to_watchlist(loser["base"], loser["symbol"], f"loser_{loser['change']:.0f}%"):
                added += 1
                logger.info(f"Loser board: added {loser['base']} ({loser['change']:+.1f}%)")

        if added:
            logger.info(f"Loser board: added {added} coins to watchlist")

    except Exception as e:
        logger.error(f"Loser board scan: {e}")


def scan_funding_extreme():
    """Scan for extreme negative funding rates — shorts paying longs.

    Strategy: when funding rate is very negative, shorts are overcrowded.
    This creates squeeze potential. Auto-adds to watchlist.
    """
    from signal_filter import EXCLUDED_SYMBOLS
    from watchlist_scanner import add_to_watchlist

    FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

    try:
        r = requests.get(FUNDING_URL, timeout=10)
        if r.status_code != 200:
            return
        data = r.json()

        extremes = []
        for item in data:
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym.replace("USDT", "")
            if base in EXCLUDED_SYMBOLS:
                continue
            funding = float(item.get("lastFundingRate", 0))
            # Extreme negative funding: shorts paying > 0.1% per 8h
            if funding <= -0.001:
                extremes.append({
                    "symbol": sym, "base": base,
                    "funding": funding,
                    "funding_pct": round(funding * 100, 4),
                })

        extremes.sort(key=lambda x: x["funding"])
        added = 0
        for ex in extremes[:10]:
            if add_to_watchlist(ex["base"], ex["symbol"], f"funding_{ex['funding_pct']}%"):
                added += 1
                logger.info(f"Funding extreme: {ex['base']} rate={ex['funding_pct']}%")

        if added:
            logger.info(f"Funding extreme: added {added} coins to watchlist")

    except Exception as e:
        logger.error(f"Funding scan: {e}")


def scan_candidates():
    """Get candidate symbols to scan — recently active + shortlisted."""
    # Scan symbols that appeared in alerts recently
    recent_symbols = db.q_all("""
        SELECT DISTINCT pair FROM alerts
        WHERE received_at >= datetime('now', '-48 hours')
    """)
    candidates = set(r["pair"] for r in recent_symbols if r["pair"])

    # Also scan symbols from volume rank that are small cap
    vol_rank = get_volume_rank("BTCUSDT")  # This refreshes the ticker cache
    # Add new listings
    try:
        r = requests.get(BINANCE_EXCHANGE_INFO, timeout=10)
        if r.status_code == 200:
            symbols = r.json().get("symbols", [])
            cutoff = int((time.time() - 180 * 86400) * 1000)
            for s in symbols:
                if (s.get("onboardDate", 0) > cutoff
                        and s["status"] == "TRADING"
                        and s["symbol"].endswith("USDT")):
                    candidates.add(s["symbol"])
    except Exception:
        pass

    return list(candidates)[:100]  # Cap at 100 to avoid rate limits


def scan_once():
    """Run one scan cycle."""
    candidates = scan_candidates()
    if not candidates:
        return

    logger.info(f"Scanning {len(candidates)} candidates...")
    found = 0

    for symbol in candidates:
        try:
            # Rate limit: ~2 req per symbol, 100 symbols = 200 req, ~3 req/sec = ~1min
            price = _get_price(symbol)
            if not price:
                continue
            oi_raw = _get_oi(symbol)
            if not oi_raw:
                continue

            oi_usd = oi_raw * price
            _record_oi(symbol, oi_usd)

            # Check OI delta
            delta_1h = _get_oi_delta(symbol, hours=1)
            if not delta_1h:
                time.sleep(0.2)
                continue

            reasons = []
            warnings = []
            score = 0

            # ── Stealth accumulation: OI up > 5% but price flat ──
            oi_pct = delta_1h["delta_pct"]
            # Get price change
            ta = analyze(symbol)
            if not ta:
                time.sleep(0.2)
                continue

            price_change = ta.get("price_change_pct", 0)

            if oi_pct >= 5 and abs(price_change) < 3:
                reasons.append(f"掃描:OI悄漲+{oi_pct}%(價{price_change:+.1f}%)")
                score += 3

            elif oi_pct >= 10:
                reasons.append(f"掃描:OI+{oi_pct}%")
                score += 2

            # ── Volume spike with flat price ──
            vol_ratio = ta.get("volume_ratio")
            if vol_ratio and vol_ratio >= 3.0 and abs(price_change) < 5:
                reasons.append(f"掃描:異常放量{vol_ratio}x(價平)")
                score += 2

            # ── Cross-exchange: both Binance + Hype OI up ──
            hype = fetch_hype_data(symbol)
            if hype and hype.get("oi_usd", 0) > 100_000 and oi_pct >= 5:
                reasons.append(f"掃描:雙所OI(Hype ${hype['oi_usd']/1e3:.0f}K)")
                score += 2

            if not reasons:
                time.sleep(0.2)
                continue

            # Add TA patterns — only bullish ones
            patterns = ta.get("patterns", [])
            bearish_keywords = ["下跌", "頂背離", "死叉", "高檔鈍化"]
            bullish = [p for p in patterns
                       if ("金叉" in p or "底背離" in p or "共振" in p or "低檔鈍化" in p)
                       and not any(bk in p for bk in bearish_keywords)]
            for p in bullish:
                reasons.append(p)
                score += 1

            # Bearish patterns — reduce score
            bearish = [p for p in patterns
                       if any(bk in p for bk in bearish_keywords)]
            for p in bearish:
                warnings.append(p)
                score -= 1

            # Smart money
            sm = ta.get("smart_money")
            if sm:
                for tag in sm.get("tags", []):
                    reasons.append(tag)
                score += sm.get("score", 0)

            # Dedup with existing signals
            existing = db.q_val(
                "SELECT COUNT(*) FROM signals WHERE pair=? AND created_at >= datetime('now', '-1 hours')",
                (symbol,), default=0,
            )
            if existing:
                time.sleep(0.2)
                continue

            # Build and save signal
            alert_data = {
                "oi_change_pct": oi_pct,
                "price_change_pct": price_change,
                "oi_usd": oi_usd,
                "market_cap_usd": None,
            }
            sig = _build_scanner_signal(symbol, reasons, score, ta, alert_data, warnings)
            if sig and sig["score"] >= 4:
                found += 1
                logger.info(f"Scanner signal: {symbol} score={sig['score']} reasons={reasons}")

                # Save to DB (minimal — reuse existing signal structure)
                _save_scanner_signal(sig)

                # Notify via TG
                send_signal(sig)

            time.sleep(0.3)  # Rate limit

        except Exception as e:
            logger.error(f"Scan {symbol}: {e}")
            time.sleep(0.2)

    logger.info(f"Scan complete: {found} signals from {len(candidates)} candidates")


def _save_scanner_signal(sig: dict):
    """Save scanner signal to DB."""
    import json
    ta = sig.get("ta", {})
    tp_sl = sig.get("tp_sl", {})

    db.execute("""
        INSERT INTO signals (symbol, pair, level, reasons, warnings,
            rsi, volume_ratio, entry_price,
            signal_type, confidence, score,
            sl, sl_pct, tp1, tp1_pct, tp2, tp2_pct, tp3, tp3_pct, tp4_trail, tp4_trail_pct, rr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig["symbol"], sig["pair"], sig["level"],
        json.dumps(sig.get("reasons", []), ensure_ascii=False),
        json.dumps(sig.get("warnings", []), ensure_ascii=False),
        ta.get("rsi"), ta.get("volume_ratio"), ta.get("current_price"),
        "scanner", sig.get("confidence", ""), sig.get("score", 0),
        tp_sl.get("sl"), tp_sl.get("sl_pct"),
        tp_sl.get("tp1"), tp_sl.get("tp1_pct"),
        tp_sl.get("tp2"), tp_sl.get("tp2_pct"),
        tp_sl.get("tp3"), tp_sl.get("tp3_pct"),
        tp_sl.get("tp4_trail"), tp_sl.get("tp4_trail_pct"),
        tp_sl.get("rr"),
    ))


def run_scanner_loop(interval: int = SCAN_INTERVAL):
    """Run scanner every N seconds."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logger.info(f"Scanner started, scanning every {interval}s")

    # Wait a bit for initial data to accumulate
    time.sleep(30)

    cycle = 0
    while True:
        try:
            scan_once()

            # Run loser board + funding scan every 3rd cycle (~15 min)
            if cycle % 3 == 0:
                scan_losers_board()
                scan_funding_extreme()

            cycle += 1
        except Exception as e:
            logger.error(f"Scanner loop error: {e}")
        time.sleep(interval)
