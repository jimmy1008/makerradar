"""TG Channel Listener — monitors channels for alerts using telethon."""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events

import db
from config import Config
from tg_parser import parse_message
from signal_filter import evaluate
from tg_bot import send_signal

TW = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


def _save_alert(alert, raw_text: str):
    """Save raw alert to DB."""
    db.execute("""
        INSERT INTO alerts (symbol, pair, source, oi_change_pct, price_change_pct,
            time_window_sec, oi_usd, oi_mcap_ratio, price_24h_pct, market_cap_usd, raw_text,
            ru7_score, ru7_tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        alert.symbol, alert.pair, alert.source,
        alert.oi_change_pct, alert.price_change_pct,
        alert.time_window_sec, alert.oi_usd,
        alert.oi_mcap_ratio, alert.price_24h_pct,
        alert.market_cap_usd, raw_text,
        alert.ru7_score,
        json.dumps(alert.ru7_tags, ensure_ascii=False) if alert.ru7_tags else None,
    ))


def _save_signal(sig: dict):
    """Save evaluated signal to DB."""
    ta = sig.get("ta", {})
    kdj = ta.get("kdj", {})
    alert = sig.get("alert", {})

    vol_rank = sig.get("vol_rank") or {}

    patterns = ta.get("patterns", [])

    entry_price = ta.get("current_price")
    tp_sl = sig.get("tp_sl", {})
    social = sig.get("social", {})
    smart_money = ta.get("smart_money") or {}

    db.execute("""
        INSERT INTO signals (symbol, pair, level, reasons, warnings,
            rsi, kdj_k, kdj_d, kdj_j, volume_ratio,
            oi_change_pct, price_change_pct, oi_usd, oi_mcap_ratio, market_cap_usd,
            patterns, signal_type, confidence, score, listing_age, vol_percentile,
            chase_threshold, hit_count,
            social_score, social_tags, trending_rank, vol_rank_delta,
            ru7_score, ru7_tags,
            funding_rate, ls_ratio, top_ls_ratio, hype_oi_usd, hype_funding,
            entry_price,
            sl, sl_pct, tp1, tp1_pct, tp2, tp2_pct, tp3, tp3_pct, tp4_trail, tp4_trail_pct, rr)
        VALUES (""" + ",".join(["?"] * 46) + """)
    """, (
        sig["symbol"], sig["pair"], sig["level"],
        json.dumps(sig.get("reasons", []), ensure_ascii=False),
        json.dumps(sig.get("warnings", []), ensure_ascii=False),
        ta.get("rsi"), kdj.get("K"), kdj.get("D"), kdj.get("J"),
        ta.get("volume_ratio"),
        alert.get("oi_change_pct"), alert.get("price_change_pct"),
        alert.get("oi_usd"), alert.get("oi_mcap_ratio"),
        alert.get("market_cap_usd"),
        json.dumps(patterns, ensure_ascii=False),
        sig.get("signal_type", "opportunity"),
        sig.get("confidence", ""), sig.get("score", 0),
        sig.get("listing_age"), vol_rank.get("percentile"),
        sig.get("chase_threshold"), sig.get("hit_count", 0),
        social.get("social_score", 0),
        json.dumps(social.get("social_tags", []), ensure_ascii=False),
        social.get("trending_rank"),
        social.get("vol_rank_delta"),
        alert.get("ru7_score"),
        json.dumps(alert.get("ru7_tags", []), ensure_ascii=False) if alert.get("ru7_tags") else None,
        smart_money.get("funding_rate") if smart_money else None,
        smart_money.get("ls_ratio") if smart_money else None,
        smart_money.get("top_ls_ratio") if smart_money else None,
        smart_money.get("hype_oi_usd") if smart_money else None,
        smart_money.get("hype_funding") if smart_money else None,
        entry_price,
        tp_sl.get("sl"), tp_sl.get("sl_pct"),
        tp_sl.get("tp1"), tp_sl.get("tp1_pct"),
        tp_sl.get("tp2"), tp_sl.get("tp2_pct"),
        tp_sl.get("tp3"), tp_sl.get("tp3_pct"),
        tp_sl.get("tp4_trail"), tp_sl.get("tp4_trail_pct"),
        tp_sl.get("rr"),
    ))


# Dedup: skip if same symbol signaled within N seconds
_DEDUP_SECONDS = 300
_recent_signals: dict[str, float] = {}


def _is_duplicate(symbol: str) -> bool:
    now = datetime.now(TW).timestamp()
    last = _recent_signals.get(symbol, 0)
    if now - last < _DEDUP_SECONDS:
        return True
    _recent_signals[symbol] = now
    return False


async def _handle_message(event):
    """Process incoming channel message."""
    text = event.message.message
    if not text:
        return

    alert = parse_message(text)
    if not alert:
        return

    logger.info(f"Alert parsed: {alert.symbol} [{alert.source}]")
    _save_alert(alert, text)

    # Check dedup
    if _is_duplicate(alert.symbol):
        logger.debug(f"{alert.symbol}: duplicate within {_DEDUP_SECONDS}s, skip")
        return

    # Evaluate
    sig = evaluate(alert)
    if not sig:
        logger.debug(f"{alert.symbol}: filtered out")
        return

    logger.info(f"Signal: {sig['symbol']} [{sig['level']}] reasons={sig['reasons']}")
    _save_signal(sig)

    # Notify
    sent = send_signal(sig)
    if sent:
        db.execute("UPDATE signals SET notified=1 WHERE id=(SELECT MAX(id) FROM signals)")


async def run_listener():
    """Start telethon client and listen to configured channels."""
    cfg = Config()
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        logger.error("TG API credentials not configured")
        return

    session_path = str(db.DB_PATH.parent / "tg_session")
    client = TelegramClient(session_path, cfg.tg_api_id, cfg.tg_api_hash)

    await client.start()
    logger.info("Telethon client started")

    # Resolve channel entities
    channels = []
    for ch in cfg.tg_channels:
        try:
            entity = await client.get_entity(ch)
            channels.append(entity)
            logger.info(f"Monitoring channel: {ch}")
        except Exception as e:
            logger.error(f"Failed to resolve channel {ch}: {e}")

    if not channels:
        logger.error("No channels resolved, exiting")
        return

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            await _handle_message(event)
        except Exception as e:
            logger.error(f"Handler error: {e}", exc_info=True)

    logger.info(f"Listening to {len(channels)} channels...")
    await client.run_until_disconnected()


def start_listener_thread():
    """Run the listener in a background thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_listener())
    except Exception as e:
        logger.error(f"Listener thread error: {e}", exc_info=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_listener())
