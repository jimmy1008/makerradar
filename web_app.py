"""MakerRadar — Binance Futures Signal Monitor."""

import json
import threading
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

import db
from signal_filter import FILTERS

BASE = Path(__file__).parent

app = FastAPI(title="MakerRadar")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _utc_to_tw(utc_str: str) -> str:
    """Convert UTC datetime string to Taiwan time (UTC+8), return MM-DD HH:MM."""
    if not utc_str or len(utc_str) < 16:
        return utc_str or ""
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromisoformat(utc_str.replace(" ", "T"))
        tw = dt + timedelta(hours=8)
        return tw.strftime("%m-%d %H:%M")
    except Exception:
        return utc_str[5:16] if utc_str else ""


templates.env.filters["tw"] = _utc_to_tw

_listener_thread: threading.Thread | None = None
_tracker_thread: threading.Thread | None = None
_scanner_thread: threading.Thread | None = None
_watchlist_thread: threading.Thread | None = None


@app.on_event("startup")
async def _on_startup():
    global _listener_thread
    from config import Config
    cfg = Config()

    # ── Disabled: original OI signal listener ──
    # if cfg.tg_api_id and cfg.tg_api_hash and cfg.tg_channels:
    #     from tg_listener import start_listener_thread
    #     _listener_thread = threading.Thread(target=start_listener_thread, daemon=True)
    #     _listener_thread.start()
    #     print(f"[MakerRadar] Listening to {len(cfg.tg_channels)} TG channels")
    print("[MakerRadar] OI listener DISABLED")

    # Price tracker (fills in 1h/4h/24h price data + MFE)
    from tracker import run_tracker_loop
    _tracker_thread = threading.Thread(target=run_tracker_loop, args=(300,), daemon=True)
    _tracker_thread.start()
    print("[MakerRadar] Price tracker started (every 5min)")

    # ── Disabled: old OI scanner ──
    # from scanner import run_scanner_loop
    # _scanner_thread = threading.Thread(target=run_scanner_loop, args=(300,), daemon=True)
    # _scanner_thread.start()
    print("[MakerRadar] OI scanner DISABLED")

    # ── Disabled: original watchlist scanner ──
    # from watchlist_scanner import run_watchlist_loop
    # _watchlist_thread = threading.Thread(target=run_watchlist_loop, args=(900,), daemon=True)
    # _watchlist_thread.start()
    print("[MakerRadar] Old watchlist scanner DISABLED")

    print("[MakerRadar] Lana Clone moved to ~/Desktop/LanaClone")


def _build_dashboard(page: int = 1, per_page: int = 50) -> dict:
    total_count = q_val("SELECT COUNT(*) FROM signals", default=0)
    offset = (page - 1) * per_page

    signals = db.q_all(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (per_page, offset),
    )
    for s in signals:
        try:
            reasons = json.loads(s.get("reasons") or "[]")
            s["reasons_text"] = " | ".join(reasons)
        except Exception:
            s["reasons_text"] = s.get("reasons", "")
        try:
            patterns = json.loads(s.get("patterns") or "[]")
            s["patterns_text"] = " ".join(patterns)
        except Exception:
            s["patterns_text"] = s.get("patterns", "")
        try:
            stags = json.loads(s.get("social_tags") or "[]")
            s["social_tags_text"] = " ".join(stags)
        except Exception:
            s["social_tags_text"] = s.get("social_tags", "")

    recent_alerts = db.q_all(
        "SELECT * FROM alerts ORDER BY received_at DESC LIMIT 30"
    )

    today_signals = db.q_val(
        "SELECT COUNT(*) FROM signals WHERE created_at >= date('now')", default=0
    )
    strong_count = db.q_val(
        "SELECT COUNT(*) FROM signals WHERE level='strong'", default=0
    )
    total_alerts = db.q_val("SELECT COUNT(*) FROM alerts", default=0)

    from tracker import get_confidence_stats
    stats = get_confidence_stats()

    return {
        "signals": signals,
        "recent_alerts": recent_alerts,
        "stats": stats,
        "total_count": total_count,
        "today_signals": today_signals,
        "strong_count": strong_count,
        "total_alerts": total_alerts,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total_count + per_page - 1) // per_page),
        "filters": FILTERS,
        "listener_running": _listener_thread is not None and _listener_thread.is_alive(),
    }


def q_val(sql, params=(), default=None):
    return db.q_val(sql, params, default) or default


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = Query(1)):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "d": _build_dashboard(page),
    })


@app.get("/partial/signals", response_class=HTMLResponse)
async def partial_signals(request: Request, page: int = Query(1)):
    return templates.TemplateResponse("_signals.html", {
        "request": request,
        "d": _build_dashboard(page),
    })


def _build_backtest() -> dict:
    from tracker import get_confidence_stats, get_signal_type_stats

    # Overall stats
    total = db.q_val("SELECT COUNT(*) FROM signals", default=0) or 0
    tracked = db.q_val("SELECT COUNT(*) FROM signals WHERE high_since IS NOT NULL", default=0) or 0

    # TP/SL hit detection — uses result column filled by tracker via kline high/low
    tp_hit = db.q_val("""
        SELECT COUNT(*) FROM signals WHERE result IN ('tp1','tp2','tp3')
    """, default=0) or 0
    sl_hit = db.q_val("""
        SELECT COUNT(*) FROM signals WHERE result = 'sl'
    """, default=0) or 0
    be_hit = db.q_val("""
        SELECT COUNT(*) FROM signals WHERE result = 'be'
    """, default=0) or 0

    decided = tp_hit + sl_hit
    win_rate = (tp_hit / decided * 100) if decided > 0 else None
    avg_rr = db.q_val("SELECT AVG(rr) FROM signals WHERE rr IS NOT NULL", default=0)

    # Expectancy based on actual TP/SL pct
    # avg win = weighted by which TP was hit
    avg_win_pct = db.q_val("""
        SELECT AVG(
            CASE result
                WHEN 'tp1' THEN tp1_pct
                WHEN 'tp2' THEN tp2_pct
                WHEN 'tp3' THEN tp3_pct
            END
        ) FROM signals WHERE result IN ('tp1','tp2','tp3')
    """, default=0)
    avg_loss_pct = db.q_val("""
        SELECT AVG(sl_pct) FROM signals WHERE result = 'sl'
    """, default=0)

    if win_rate and avg_win_pct and avg_loss_pct:
        expectancy = (win_rate / 100) * avg_win_pct - (1 - win_rate / 100) * avg_loss_pct
    else:
        expectancy = None

    # By confidence with TP/SL stats
    by_conf = get_confidence_stats()
    for st in by_conf:
        conf = st["confidence"]
        st["tp_hit"] = db.q_val("""
            SELECT COUNT(*) FROM signals
            WHERE confidence=? AND result IN ('tp1','tp2','tp3')
        """, (conf,), default=0) or 0
        st["sl_hit"] = db.q_val("""
            SELECT COUNT(*) FROM signals
            WHERE confidence=? AND result = 'sl'
        """, (conf,), default=0) or 0
        d = st["tp_hit"] + st["sl_hit"]
        st["tp_win_rate"] = (st["tp_hit"] / d * 100) if d > 0 else None

    by_type = get_signal_type_stats()

    # Recent individual results
    recent = db.q_all("""
        SELECT * FROM signals
        WHERE entry_price IS NOT NULL
        ORDER BY created_at DESC LIMIT 50
    """)
    for s in recent:
        # result column is already filled by tracker ('tp1','tp2','tp3','sl' or NULL)
        if not s.get("result"):
            if s.get("high_since") is None:
                s["result"] = "pending"
            else:
                s["result"] = "open"

    return {
        "total": total,
        "tracked": tracked,
        "tp_hit": tp_hit,
        "sl_hit": sl_hit,
        "win_rate": win_rate,
        "avg_rr": avg_rr,
        "expectancy": expectancy,
        "by_confidence": by_conf,
        "by_type": by_type,
        "recent": recent,
    }


def _build_digest(date: str = "", coin: str = "") -> dict:
    from datetime import datetime, timezone, timedelta
    TW = timezone(timedelta(hours=8))

    if not date:
        date = datetime.now(TW).strftime("%Y-%m-%d")

    # Available dates
    available = db.q_all(
        "SELECT DISTINCT substr(received_at, 1, 10) as d FROM alerts ORDER BY d DESC LIMIT 30"
    )
    available_dates = [r["d"] for r in available if r["d"]]

    # Coin aggregation for the date
    coins_raw = db.q_all("""
        SELECT
            symbol,
            pair,
            COUNT(*) as mention_count,
            SUM(CASE WHEN source='oi_price' THEN 1 ELSE 0 END) as oi_count,
            SUM(CASE WHEN source='price_surge' THEN 1 ELSE 0 END) as surge_count,
            MIN(received_at) as first_seen,
            MAX(received_at) as last_seen,
            MAX(market_cap_usd) as mcap
        FROM alerts
        WHERE received_at >= ? AND received_at < date(?, '+1 day')
        GROUP BY symbol
        ORDER BY mention_count DESC
    """, (date, date))

    # Enrich with signal data
    for c in coins_raw:
        sig = db.q_one("""
            SELECT confidence, score FROM signals
            WHERE symbol=? AND created_at >= ? AND created_at < date(?, '+1 day')
            ORDER BY score DESC LIMIT 1
        """, (c["symbol"], date, date))
        c["best_confidence"] = sig["confidence"] if sig else ""
        c["signal_count"] = db.q_val("""
            SELECT COUNT(*) FROM signals
            WHERE symbol=? AND created_at >= ? AND created_at < date(?, '+1 day')
        """, (c["symbol"], date, date), default=0) or 0

    # Detail for selected coin
    detail = []
    if coin:
        detail = db.q_all("""
            SELECT * FROM alerts
            WHERE symbol=? AND received_at >= ? AND received_at < date(?, '+1 day')
            ORDER BY received_at DESC
        """, (coin, date, date))

    return {
        "date": date,
        "available_dates": available_dates,
        "coins": coins_raw,
        "unique_coins": len(coins_raw),
        "total_alerts": sum(c["mention_count"] for c in coins_raw),
        "total_signals": sum(c["signal_count"] for c in coins_raw),
        "selected": coin,
        "detail": detail,
    }


@app.get("/partial/digest", response_class=HTMLResponse)
async def partial_digest(request: Request, date: str = "", coin: str = ""):
    return templates.TemplateResponse("_digest.html", {
        "request": request,
        "dg": _build_digest(date, coin),
    })


@app.get("/partial/backtest", response_class=HTMLResponse)
async def partial_backtest(request: Request):
    return templates.TemplateResponse("_backtest.html", {
        "request": request,
        "b": _build_backtest(),
    })


@app.get("/partial/watchlist", response_class=HTMLResponse)
async def partial_watchlist(request: Request):
    coins = db.q_all("""
        SELECT * FROM watchlist WHERE status='watching'
        ORDER BY entry_triggered DESC, updated_at DESC
    """)
    entries = db.q_all("""
        SELECT * FROM watchlist WHERE entry_triggered=1
        ORDER BY entry_at DESC LIMIT 20
    """)
    return templates.TemplateResponse("_watchlist.html", {
        "request": request,
        "coins": coins,
        "entries": entries,
        "total": len(coins),
    })


@app.get("/api/watchlist/add")
async def api_watchlist_add(symbol: str, pair: str = ""):
    from watchlist_scanner import add_to_watchlist
    if not pair:
        pair = symbol.upper() + "USDT"
    added = add_to_watchlist(symbol.upper(), pair.upper(), source="manual")
    return {"added": added, "symbol": symbol.upper()}


@app.get("/api/watchlist/remove")
async def api_watchlist_remove(symbol: str):
    from watchlist_scanner import remove_from_watchlist
    remove_from_watchlist(symbol.upper())
    return {"removed": True, "symbol": symbol.upper()}


@app.get("/api/watchlist/scan")
async def api_watchlist_scan(symbol: str):
    """Manually trigger analysis for a single watchlist coin."""
    from indicators import analyze_for_entry
    pair = symbol.upper() + "USDT"
    result = analyze_for_entry(pair)
    if not result:
        return {"error": f"No data for {symbol}"}
    # Simplify for JSON
    result["trend"] = {
        "trend": result["trend"]["trend"],
        "strength": result["trend"]["strength"],
        "last_swing_low": result["trend"].get("last_swing_low"),
        "last_swing_high": result["trend"].get("last_swing_high"),
        "above_structure": result["trend"].get("above_structure"),
    }
    return result


@app.get("/api/stats")
async def stats():
    from tracker import get_confidence_stats, get_signal_type_stats
    return {
        "by_confidence": get_confidence_stats(),
        "by_type": get_signal_type_stats(),
    }


@app.get("/api/status")
async def status():
    d = _build_dashboard()
    return {
        "listener": d["listener_running"],
        "total_signals": d["total_count"],
        "today_signals": d["today_signals"],
        "total_alerts": d["total_alerts"],
    }



if __name__ == "__main__":
    import uvicorn
    from config import Config
    uvicorn.run(app, host="127.0.0.1", port=Config.port)
