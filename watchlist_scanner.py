"""Watchlist scanner — continuously monitors tracked coins for entry setups.

Two entry modes:
  1. Reversal: downtrend + momentum decay + bullish candle (FRAX pattern)
  2. Pullback: uptrend + retrace to Fib/support + bounce confirmation (RIVER pattern)

Every scan saves a factor snapshot for future weight optimization.
"""

import time
import json
import logging
from datetime import datetime, timezone, timedelta

import db
from indicators import analyze_for_entry
from binance_meta import get_listing_age_days
from tg_bot import send_watchlist_entry

logger = logging.getLogger(__name__)
TW = timezone(timedelta(hours=8))

# Don't re-alert same coin within 4 hours
_ENTRY_COOLDOWN = 4 * 3600
_recent_entries: dict[str, float] = {}


def add_to_watchlist(symbol: str, pair: str, source: str = "signal"):
    """Add a coin to the watchlist if not already there."""
    from signal_filter import EXCLUDED_SYMBOLS
    if symbol.upper() in EXCLUDED_SYMBOLS:
        return False

    existing = db.q_one(
        "SELECT id, status FROM watchlist WHERE symbol=? AND status='watching'",
        (symbol,),
    )
    if existing:
        return False

    db.execute("""
        INSERT INTO watchlist (symbol, pair, source, status)
        VALUES (?, ?, ?, 'watching')
    """, (symbol, pair, source))
    logger.info(f"Watchlist: added {symbol}")
    return True


def remove_from_watchlist(symbol: str):
    """Remove a coin from active watching."""
    db.execute(
        "UPDATE watchlist SET status='removed', updated_at=datetime('now') WHERE symbol=? AND status='watching'",
        (symbol,),
    )


def _save_factor_snapshot(analysis: dict, coin: dict, entry_type: str):
    """Save all factors at the moment of entry detection for future optimization."""
    trend = analysis["trend"]
    reversal = analysis.get("reversal", {})
    pullback = analysis.get("pullback", {})
    accumulation = analysis.get("accumulation", {})
    decay = reversal.get("momentum_decay", {})
    sl = analysis.get("structure_sl", {})

    # Candle metrics
    cur_price = analysis["current_price"]
    atr = analysis.get("atr")
    atr_pct = (atr / cur_price * 100) if atr and cur_price else None

    # Compute drop from period high
    swing_highs = trend.get("swing_highs", [])
    period_high = max((p for _, p in swing_highs), default=cur_price)
    drop_pct = (period_high - cur_price) / period_high * 100 if period_high > 0 else 0

    listing_age = get_listing_age_days(coin["pair"])

    # TP1 from tp_options
    tp_opts = analysis.get("tp_options", {})
    tp1_info = None
    for key in ["fib_1618", "fib_1000", "prev_high", "period_high", "atr_trail"]:
        if key in tp_opts and tp_opts[key].get("pct", 0) > 0:
            tp1_info = tp_opts[key]
            break

    db.execute("""
        INSERT INTO factor_snapshots (
            symbol, pair, scan_type,
            trend, trend_strength,
            rsi, kdj_k, kdj_d, kdj_j,
            volume_ratio, atr, atr_pct,
            decay_score, reversal_score, pullback_score,
            candle_body_ratio, candle_vs_avg,
            drop_from_high_pct, pullback_depth_pct, fib_level,
            above_structure,
            swing_low, swing_high,
            listing_age_days,
            entry_score, entry_type, entry_price,
            sl_price, sl_pct,
            tp1_price, tp1_pct
        ) VALUES (""" + ",".join(["?"] * 31) + ")",
        (
            coin["symbol"], coin["pair"], entry_type,
            trend["trend"], trend.get("strength", 0),
            analysis.get("rsi"),
            analysis["kdj"]["K"] if analysis.get("kdj") else None,
            analysis["kdj"]["D"] if analysis.get("kdj") else None,
            analysis["kdj"]["J"] if analysis.get("kdj") else None,
            reversal.get("vol_ratio") or accumulation.get("vol_growth"),
            atr, atr_pct,
            decay.get("score", 0),
            reversal.get("score", 0),
            pullback.get("score", 0),
            None,  # candle_body_ratio (computed internally)
            None,  # candle_vs_avg
            round(drop_pct, 2),
            pullback.get("drop_pct"),
            pullback.get("fib_level"),
            1 if trend.get("above_structure") else 0,
            trend.get("last_swing_low"),
            trend.get("last_swing_high"),
            listing_age,
            analysis.get("entry_score", 0),
            entry_type,
            cur_price,
            sl.get("sl"),
            sl.get("sl_pct"),
            tp1_info["price"] if tp1_info else None,
            tp1_info["pct"] if tp1_info else None,
        ),
    )


def scan_watchlist():
    """Scan all watchlist coins for entry setups."""
    coins = db.q_all(
        "SELECT * FROM watchlist WHERE status='watching' ORDER BY added_at DESC"
    )

    if not coins:
        return

    now_ts = time.time()
    entries_found = 0

    for coin in coins:
        symbol = coin["symbol"]
        pair = coin["pair"]

        # Cooldown check
        last_entry = _recent_entries.get(symbol, 0)
        if now_ts - last_entry < _ENTRY_COOLDOWN:
            continue

        try:
            analysis = analyze_for_entry(pair)
        except Exception as e:
            logger.error(f"Watchlist scan {symbol}: {e}")
            continue

        if not analysis:
            continue

        trend = analysis["trend"]
        entry_type = analysis.get("entry_type")
        entry_score = analysis.get("entry_score", 0)

        # Update watchlist state
        db.execute("""
            UPDATE watchlist SET
                trend=?, last_scan=datetime('now'),
                swing_low_4h=?, swing_high_4h=?,
                updated_at=datetime('now')
            WHERE symbol=? AND status='watching'
        """, (
            trend["trend"],
            trend.get("last_swing_low"),
            trend.get("last_swing_high"),
            symbol,
        ))

        # Check for entry trigger
        if entry_type:
            _recent_entries[symbol] = now_ts
            entries_found += 1

            # Save factor snapshot
            try:
                _save_factor_snapshot(analysis, coin, entry_type)
            except Exception as e:
                logger.error(f"Factor snapshot {symbol}: {e}")

            # Build entry signal with clear instructions
            reversal = analysis.get("reversal", {})
            pullback = analysis.get("pullback", {})
            accumulation = analysis.get("accumulation", {})
            if entry_type == "reversal":
                active_signals = reversal["signals"]
            elif entry_type == "pullback":
                active_signals = pullback.get("signals", [])
            else:
                active_signals = accumulation.get("signals", [])

            entry_sig = {
                "symbol": symbol,
                "pair": pair,
                "price": analysis["current_price"],
                "trend": trend["trend"],
                "entry_type": entry_type,
                "entry_score": entry_score,
                "reversal": reversal,
                "pullback": pullback,
                "signals": active_signals,
                "rsi": analysis.get("rsi"),
                "kdj": analysis.get("kdj"),
                "structure_sl": analysis.get("structure_sl"),
                "tp_options": analysis.get("tp_options"),
                "atr": analysis.get("atr"),
            }

            # Save entry trigger
            db.execute("""
                UPDATE watchlist SET
                    entry_triggered=1, entry_price=?, entry_at=datetime('now'),
                    notes=?, updated_at=datetime('now')
                WHERE symbol=? AND status='watching'
            """, (
                analysis["current_price"],
                json.dumps(active_signals, ensure_ascii=False),
                symbol,
            ))

            logger.info(f"ENTRY [{entry_type}]: {symbol} score={entry_score} — {active_signals}")

            # Send TG notification
            try:
                send_watchlist_entry(entry_sig)
            except Exception as e:
                logger.error(f"TG send {symbol}: {e}")

    if entries_found:
        logger.info(f"Watchlist scan: {entries_found} entries found from {len(coins)} coins")


def run_watchlist_loop(interval: int = 900):
    """Run watchlist scanner every N seconds (default 15 min for 4H data)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logger.info(f"Watchlist scanner started, polling every {interval}s")
    while True:
        try:
            scan_watchlist()
        except Exception as e:
            logger.error(f"Watchlist scan error: {e}")
        time.sleep(interval)
