"""TG Bot — sends signal notifications via Telegram Bot API."""

import logging
import requests

from config import Config

logger = logging.getLogger(__name__)


def send_signal(signal: dict) -> bool:
    """Send a formatted signal message to the configured chat."""
    cfg = Config()
    if not cfg.tg_bot_token or not cfg.tg_chat_id:
        return False

    msg = _format_signal(signal)
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/sendMessage"

    try:
        r = requests.post(url, json={
            "chat_id": cfg.tg_chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if r.status_code == 200:
            logger.info(f"Signal sent: {signal['symbol']} [{signal['level']}]")
            return True
        else:
            logger.error(f"TG send failed: {r.status_code} {r.text}")
            return False
    except Exception as e:
        logger.error(f"TG send error: {e}")
        return False


def _format_signal(sig: dict) -> str:
    """Signal message — unified format with entry format."""
    conf = sig.get("confidence", "low")
    light = {"high": "\U0001f7e2", "mid": "\U0001f7e1", "low": "\U0001f534"}
    ta = sig.get("ta", {})
    tp_sl = sig.get("tp_sl", {})
    entry = ta.get("current_price")
    score = sig.get("score", 0)
    reasons = sig.get("reasons", [])

    lines = [
        f"{light.get(conf, light['low'])} <b>{sig['symbol']}</b> — OI信號 (score {score})",
        f"做多 {entry:.6g}" if entry else "",
    ]

    if reasons:
        lines.append(f"<b>訊號:</b> {' | '.join(reasons[:4])}")

    warnings = sig.get("warnings", [])
    if warnings:
        lines.append(f"\u26a0\ufe0f {' | '.join(warnings[:3])}")

    rsi = ta.get("rsi")
    if rsi:
        rsi_text = f"RSI: {rsi:.0f}"
        kdj = ta.get("kdj")
        if kdj:
            rsi_text += f"  K: {kdj['K']:.0f}  J: {kdj['J']:.0f}"
        lines.append(rsi_text)

    if tp_sl:
        lines.append("")
        lines.append(f"SL: {tp_sl['sl']:.6g} (-{tp_sl['sl_pct']}%)")
        rr1 = tp_sl['tp1_pct'] / tp_sl['sl_pct'] if tp_sl.get('sl_pct') else 0
        rr2 = tp_sl['tp2_pct'] / tp_sl['sl_pct'] if tp_sl.get('sl_pct') else 0
        rr3 = tp_sl['tp3_pct'] / tp_sl['sl_pct'] if tp_sl.get('sl_pct') else 0
        lines.append(f"TP1: {tp_sl['tp1']:.6g} (+{tp_sl['tp1_pct']}%) RR={rr1:.1f}")
        lines.append(f"TP2: {tp_sl['tp2']:.6g} (+{tp_sl['tp2_pct']}%) RR={rr2:.1f}")
        lines.append(f"TP3: {tp_sl['tp3']:.6g} (+{tp_sl['tp3_pct']}%) RR={rr3:.1f}")

    lines.append("")
    lines.append(f"<a href='https://www.binance.com/zh-TW/futures/{sig['pair']}'>開啟幣安</a>")

    return "\n".join(lines)


def send_watchlist_entry(entry: dict) -> bool:
    """Send a watchlist entry signal — reversal detected on tracked coin."""
    cfg = Config()
    if not cfg.tg_bot_token or not cfg.tg_chat_id:
        return False

    msg = _format_entry(entry)
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/sendMessage"

    try:
        r = requests.post(url, json={
            "chat_id": cfg.tg_chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if r.status_code == 200:
            logger.info(f"Entry sent: {entry['symbol']}")
            return True
        else:
            logger.error(f"TG entry send failed: {r.status_code} {r.text}")
            return False
    except Exception as e:
        logger.error(f"TG entry send error: {e}")
        return False


def _format_entry(entry: dict) -> str:
    """Format watchlist entry signal — clear actionable instructions."""
    symbol = entry["symbol"]
    price = entry["price"]
    signals = entry.get("signals", [])
    sl_info = entry.get("structure_sl")
    tp_opts = entry.get("tp_options", {})
    entry_type = entry.get("entry_type", "reversal")
    entry_score = entry.get("entry_score", 0)

    type_map = {
        "reversal": ("\U0001f504", "底部反轉"),
        "pullback": ("\U0001f4c8", "趨勢回踩"),
        "accumulation": ("\U0001f4a1", "量增價平"),
    }
    type_emoji, type_label = type_map.get(entry_type, ("\U0001f3af", entry_type))

    lines = [
        f"{type_emoji} <b>{symbol}</b> — {type_label} (score {entry_score})",
        f"做多 {price:.6g}",
        "",
        f"<b>訊號:</b> {' | '.join(signals)}",
    ]

    if entry.get("rsi"):
        rsi_text = f"RSI: {entry['rsi']:.0f}"
        if entry.get("kdj"):
            rsi_text += f"  K: {entry['kdj']['K']:.0f}  J: {entry['kdj']['J']:.0f}"
        lines.append(rsi_text)

    # Structure SL
    if sl_info and sl_info.get("sl_pct", 0) > 0:
        lines.append("")
        lines.append(f"SL: {sl_info['sl']:.6g} (-{sl_info['sl_pct']}%)")

    # Multi TP options — only show those with positive pct
    valid_tps = {k: v for k, v in tp_opts.items() if v.get("pct", 0) > 0} if tp_opts else {}
    if valid_tps:
        # Calculate RR for each
        sl_pct = sl_info.get("sl_pct", 1) if sl_info else 1
        lines.append("")
        for key, tp in sorted(valid_tps.items(), key=lambda x: x[1]["pct"]):
            rr = tp["pct"] / sl_pct if sl_pct > 0 else 0
            extra = f"  ({tp['trail_note']})" if tp.get("trail_note") else ""
            lines.append(f"TP {tp['label']}: {tp['price']:.6g} (+{tp['pct']}%) RR={rr:.1f}{extra}")

    lines.append("")
    lines.append(f"<a href='https://www.binance.com/zh-TW/futures/{entry['pair']}'>開啟幣安</a>")

    return "\n".join(lines)


def send_lana_signal(sig: dict) -> bool:
    """Send a Lana strategy signal."""
    cfg = Config()
    if not cfg.tg_bot_token or not cfg.tg_chat_id:
        return False

    msg = _format_lana(sig)
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/sendMessage"

    try:
        r = requests.post(url, json={
            "chat_id": cfg.tg_chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if r.status_code == 200:
            logger.info(f"Lana signal sent: {sig['symbol']}")
            return True
        else:
            logger.error(f"Lana TG failed: {r.status_code}")
            return False
    except Exception as e:
        logger.error(f"Lana TG error: {e}")
        return False


def _format_lana(sig: dict) -> str:
    """Format Lana strategy signal — fixed SL, aggressive TP."""
    source = sig.get("source", "")
    source_label = {
        "loser": "\U0001f4c9 跌幅榜",
        "funding": "\U0001f4b0 費率頂格",
        "loser+funding": "\U0001f525 跌幅+費率",
    }.get(source, source)

    extra = sig.get("extra", {})
    lines = [
        f"{source_label} <b>{sig['symbol']}</b> (score {sig['score']})",
        f"做多 {sig['price']:.6g}",
    ]

    if extra.get("change_24h"):
        lines.append(f"24h: {extra['change_24h']:+.1f}%")
    if extra.get("funding_pct"):
        lines.append(f"費率: {extra['funding_pct']}%")

    signals = sig.get("signals", [])
    if signals:
        lines.append(f"<b>穩定確認:</b> {' | '.join(signals[:4])}")

    if sig.get("rsi"):
        rsi_text = f"RSI: {sig['rsi']:.0f}"
        if sig.get("kdj"):
            rsi_text += f"  K: {sig['kdj']['K']:.0f}  J: {sig['kdj']['J']:.0f}"
        lines.append(rsi_text)

    lines.append("")
    lines.append(f"SL: {sig['sl']:.6g} (-{sig['sl_pct']}%) 固損")
    lines.append(f"TP1: {sig['tp1']:.6g} (+10%) RR={sig['rr1']:.1f}")
    lines.append(f"TP2: {sig['tp2']:.6g} (+30%) RR={sig['rr2']:.1f}")
    lines.append(f"TP3: {sig['tp3']:.6g} (+50%) RR={sig['rr3']:.1f}")

    lines.append("")
    lines.append(f"<a href='https://www.binance.com/zh-TW/futures/{sig['pair']}'>開啟幣安</a>")

    return "\n".join(lines)
