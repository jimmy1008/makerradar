"""Post-signal price tracker — records price at 1h/4h/24h and detects TP/SL hits."""

import time
import logging
import requests
from datetime import datetime, timezone, timedelta

import db

logger = logging.getLogger(__name__)
TW = timezone(timedelta(hours=8))

BINANCE_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"


def _get_price(symbol: str) -> float | None:
    try:
        r = requests.get(BINANCE_PRICE_URL, params={"symbol": symbol}, timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception as e:
        logger.error(f"Price fetch {symbol}: {e}")
    return None


def _get_klines_since(symbol: str, since_ms: int) -> list[dict] | None:
    """Fetch 5m klines since a given timestamp.

    Returns list of {high, low, close, timestamp} in chronological order.
    """
    try:
        r = requests.get(BINANCE_KLINES_URL, params={
            "symbol": symbol,
            "interval": "5m",
            "startTime": since_ms,
            "limit": 500,  # 5m * 500 = ~41h coverage
        }, timeout=10)
        if r.status_code != 200 or not r.json():
            return None

        return [
            {
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "timestamp": k[0],
            }
            for k in r.json()
        ]
    except Exception as e:
        logger.error(f"Kline fetch {symbol}: {e}")
        return None


def _detect_result_chronological(sig: dict, klines: list[dict]) -> tuple[str | None, float, float]:
    """Walk through klines in time order, return (result, high_since, low_since).

    First-to-hit wins: if TP is reached before SL, it's a win even if price
    later crashes through SL (because we would have already exited at TP).

    Trailing stop: once price reaches 70% of TP1 distance, SL moves to breakeven.
    This prevents "had upside but still SL'd" scenarios seen in data.
    """
    sl = sig.get("sl")
    tp1 = sig.get("tp1")
    tp2 = sig.get("tp2")
    tp3 = sig.get("tp3")
    entry = sig.get("entry_price")

    high_since = klines[0]["high"] if klines else 0
    low_since = klines[0]["low"] if klines else 0

    # Trailing stop: activate breakeven when price reaches 70% of TP1
    active_sl = sl
    if entry and tp1:
        trail_trigger = entry + (tp1 - entry) * 0.7

    for k in klines:
        high_since = max(high_since, k["high"])
        low_since = min(low_since, k["low"])

        # Trailing: if high ever reached 70% of TP1, move SL to breakeven
        if entry and tp1 and k["high"] >= trail_trigger and active_sl and active_sl < entry:
            active_sl = entry  # Move SL to breakeven

        # Check each candle: did SL or TP get hit?
        sl_hit = active_sl and k["low"] <= active_sl
        tp3_hit = tp3 and k["high"] >= tp3
        tp2_hit = tp2 and k["high"] >= tp2
        tp1_hit = tp1 and k["high"] >= tp1

        if sl_hit and tp1_hit:
            if k["close"] >= tp1:
                if tp3_hit:
                    return "tp3", high_since, low_since
                if tp2_hit:
                    return "tp2", high_since, low_since
                return "tp1", high_since, low_since
            else:
                # If SL was moved to breakeven, result is "be" not "sl"
                if active_sl == entry:
                    return "be", high_since, low_since
                return "sl", high_since, low_since

        if sl_hit:
            if active_sl == entry:
                return "be", high_since, low_since  # Breakeven exit
            return "sl", high_since, low_since

        if tp3_hit:
            return "tp3", high_since, low_since
        if tp2_hit:
            return "tp2", high_since, low_since
        if tp1_hit:
            return "tp1", high_since, low_since

    return None, high_since, low_since


def track_pending():
    """Check signals that need price tracking updates.

    Called periodically. Fills in price_1h, price_4h, price_24h
    and detects TP/SL hits using chronological kline analysis.
    """
    signals = db.q_all("""
        SELECT id, pair, created_at, entry_price,
               price_1h, price_4h, price_24h,
               sl, tp1, tp2, tp3,
               high_since, low_since, result
        FROM signals
        WHERE created_at >= datetime('now', '-25 hours')
          AND entry_price IS NOT NULL
          AND (price_1h IS NULL OR price_4h IS NULL OR price_24h IS NULL
               OR result IS NULL)
    """)

    if not signals:
        return

    now = datetime.now(timezone.utc)
    updated = 0

    for sig in signals:
        created = datetime.fromisoformat(sig["created_at"].replace(" ", "T") + "+00:00")
        age_hours = (now - created).total_seconds() / 3600
        pair = sig["pair"]
        entry = sig["entry_price"]

        price = None  # Lazy fetch for snapshot prices

        # ── Chronological TP/SL detection (every poll until decided) ──
        if sig["result"] is None:
            since_ms = int(created.timestamp() * 1000)
            klines = _get_klines_since(pair, since_ms)

            if klines:
                result, high, low = _detect_result_chronological(sig, klines)

                updates = ["high_since=?", "low_since=?"]
                params: list = [round(high, 8), round(low, 8)]

                if result:
                    hit_time = now.strftime("%Y-%m-%d %H:%M:%S")
                    updates.append("result=?")
                    params.append(result)
                    if result == "sl":
                        updates.append("sl_hit_at=?")
                    else:
                        updates.append("tp_hit_at=?")
                    params.append(hit_time)

                params.append(sig["id"])
                db.execute(
                    f"UPDATE signals SET {', '.join(updates)} WHERE id=?",
                    tuple(params),
                )
                updated += 1

        # ── 1h snapshot ──
        if sig["price_1h"] is None and age_hours >= 1:
            if price is None:
                price = _get_price(pair)
            if price:
                pnl = (price - entry) / entry * 100
                db.execute(
                    "UPDATE signals SET price_1h=?, pnl_1h=? WHERE id=?",
                    (price, round(pnl, 2), sig["id"]),
                )
                updated += 1

        # ── 4h snapshot ──
        if sig["price_4h"] is None and age_hours >= 4:
            if price is None:
                price = _get_price(pair)
            if price:
                pnl = (price - entry) / entry * 100
                db.execute(
                    "UPDATE signals SET price_4h=?, pnl_4h=? WHERE id=?",
                    (price, round(pnl, 2), sig["id"]),
                )
                updated += 1

        # ── 24h snapshot ──
        if sig["price_24h"] is None and age_hours >= 24:
            if price is None:
                price = _get_price(pair)
            if price:
                pnl = (price - entry) / entry * 100
                db.execute(
                    "UPDATE signals SET price_24h=?, pnl_24h=? WHERE id=?",
                    (price, round(pnl, 2), sig["id"]),
                )
                updated += 1

    if updated:
        logger.info(f"Tracker: updated {updated} price points")


def track_mfe():
    """Track Maximum Favorable Excursion for factor snapshots.

    MFE = highest price reached after entry, measured at 1h/4h/24h/72h.
    MAE = lowest price reached (max adverse excursion) at 24h.
    """
    snapshots = db.q_all("""
        SELECT id, pair, entry_price, created_at,
               mfe_1h, mfe_4h, mfe_24h, mfe_72h, mae_24h
        FROM factor_snapshots
        WHERE created_at >= datetime('now', '-74 hours')
          AND entry_price IS NOT NULL
          AND (mfe_1h IS NULL OR mfe_4h IS NULL OR mfe_24h IS NULL OR mfe_72h IS NULL)
    """)

    if not snapshots:
        return

    now = datetime.now(timezone.utc)

    for snap in snapshots:
        created = datetime.fromisoformat(snap["created_at"].replace(" ", "T") + "+00:00")
        age_hours = (now - created).total_seconds() / 3600
        entry = snap["entry_price"]
        pair = snap["pair"]

        # Determine which window to fill
        windows = [
            ("mfe_1h", 1), ("mfe_4h", 4), ("mfe_24h", 24), ("mfe_72h", 72),
        ]

        for col, hours in windows:
            if snap[col] is not None or age_hours < hours:
                continue

            # Fetch klines from entry to entry+hours
            since_ms = int(created.timestamp() * 1000)
            end_ms = since_ms + hours * 3600 * 1000
            try:
                r = requests.get(BINANCE_KLINES_URL, params={
                    "symbol": pair, "interval": "5m",
                    "startTime": since_ms, "endTime": end_ms, "limit": 500,
                }, timeout=10)
                if r.status_code != 200 or not r.json():
                    continue
                klines = r.json()
                high = max(float(k[2]) for k in klines)
                low = min(float(k[3]) for k in klines)
                mfe = round((high - entry) / entry * 100, 2)
                mae = round((entry - low) / entry * 100, 2)

                updates = [f"{col}=?"]
                params = [mfe]
                if col == "mfe_24h":
                    updates.append("mae_24h=?")
                    params.append(mae)
                params.append(snap["id"])
                db.execute(
                    f"UPDATE factor_snapshots SET {', '.join(updates)} WHERE id=?",
                    tuple(params),
                )
            except Exception as e:
                logger.error(f"MFE track {pair}: {e}")
                continue


def get_confidence_stats() -> list[dict]:
    """Get performance stats grouped by confidence level."""
    stats = db.q_all("""
        SELECT
            confidence,
            COUNT(*) as count,
            AVG(pnl_1h) as avg_1h,
            AVG(pnl_4h) as avg_4h,
            AVG(pnl_24h) as avg_24h,
            SUM(CASE WHEN pnl_1h > 0 THEN 1 ELSE 0 END) * 100.0 / NULLIF(SUM(CASE WHEN pnl_1h IS NOT NULL THEN 1 ELSE 0 END), 0) as win_1h,
            SUM(CASE WHEN pnl_4h > 0 THEN 1 ELSE 0 END) * 100.0 / NULLIF(SUM(CASE WHEN pnl_4h IS NOT NULL THEN 1 ELSE 0 END), 0) as win_4h,
            SUM(CASE WHEN pnl_24h > 0 THEN 1 ELSE 0 END) * 100.0 / NULLIF(SUM(CASE WHEN pnl_24h IS NOT NULL THEN 1 ELSE 0 END), 0) as win_24h,
            SUM(CASE WHEN pnl_1h IS NOT NULL THEN 1 ELSE 0 END) as tracked_1h,
            SUM(CASE WHEN pnl_4h IS NOT NULL THEN 1 ELSE 0 END) as tracked_4h,
            SUM(CASE WHEN pnl_24h IS NOT NULL THEN 1 ELSE 0 END) as tracked_24h
        FROM signals
        WHERE confidence != ''
        GROUP BY confidence
        ORDER BY
            CASE confidence WHEN 'high' THEN 1 WHEN 'mid' THEN 2 ELSE 3 END
    """)
    return stats


def get_signal_type_stats() -> list[dict]:
    """Get performance stats grouped by signal_type (opportunity vs dont_chase)."""
    stats = db.q_all("""
        SELECT
            COALESCE(signal_type, 'opportunity') as signal_type,
            COUNT(*) as count,
            AVG(pnl_1h) as avg_1h,
            AVG(pnl_4h) as avg_4h,
            AVG(pnl_24h) as avg_24h
        FROM signals
        WHERE pnl_1h IS NOT NULL
        GROUP BY signal_type
    """)
    return stats


def run_tracker_loop(interval: int = 300):
    """Run tracker every N seconds."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logger.info(f"Tracker started, polling every {interval}s")
    while True:
        try:
            track_pending()
            track_mfe()
        except Exception as e:
            logger.error(f"Tracker error: {e}")
        time.sleep(interval)
