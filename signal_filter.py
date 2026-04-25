"""Signal filtering — high frequency, high RR, front-running market makers."""

import math
import logging
from tg_parser import Alert
from indicators import analyze, calc_tp_sl
from binance_meta import get_listing_age_days, get_volume_rank
from social import get_social_score
from watchlist_scanner import add_to_watchlist
import db

logger = logging.getLogger(__name__)


# Excluded symbols — mainstream, stocks, commodities
# Focus on small/new altcoins only
EXCLUDED_SYMBOLS = {
    # Mainstream crypto
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "AVAX",
    "MATIC", "LINK", "UNI", "LTC", "BCH", "ATOM", "FIL", "APT", "ARB",
    "OP", "SUI", "SEI", "TIA", "JUP", "NEAR", "ICP", "TRX", "TON",
    "SHIB", "PEPE", "WIF", "BONK", "FLOKI",
    # US stocks
    "SPY", "QQQ", "AAPL", "GOOGL", "AMZN", "MSFT", "TSLA", "NVDA", "META",
    "NFLX", "COIN", "MSTR", "AMD", "INTC", "PLTR", "UBER", "ABNB",
    "PAYP", "CC",
    # Country ETFs
    "EWJ", "EWZ", "EWY",
    # Precious metals / commodities
    "XAU", "XAUT", "XPT", "XAG", "PAXG",
}

# ── Configurable thresholds ──
FILTERS = {
    "max_mcap": 200_000_000,       # 市值 < $200M
    "min_oi_change": 5.0,          # OI > 5% 進入監控
    "max_oi_change": 20.0,         # OI > 20% 警告可能已主升段
    "min_oi_mcap_ratio": 15.0,     # OI/市值 > 15%
    "min_price_surge": 8.0,        # 價格飆升 > 8%
    "min_volume_ratio": 1.5,       # 成交量 > 1.5x 均量
    "recent_listing_days": 180,    # 上線 180 天內
    "trending_top_pct": 20,        # 成交量 top 20%
    "oi_confirm_count": 1,         # OI 5-20% 需要 1+ 其他確認 (寬鬆)
    "oi_high_confirm_count": 2,    # OI > 20% 需要 2+ 其他確認
}


def _calc_chase_threshold(mcap: float | None, oi_mcap_ratio: float | None,
                          atr_pct: float | None = None) -> float:
    """Dynamic 'don't chase' threshold — incorporates ATR for volatile coins.

    Volatile coins (high ATR%) get a wider threshold so we don't miss them.
    """
    base = 40.0
    oi_ratio = oi_mcap_ratio if oi_mcap_ratio else 10.0
    mcap_m = (mcap / 1e6) if mcap else 50.0

    oi_factor = 1 + oi_ratio / 15
    mcap_factor = max(0.5, min(1.5, math.sqrt(mcap_m / 50)))

    threshold = base / oi_factor * mcap_factor

    # ATR adjustment: volatile coins get wider threshold
    if atr_pct and atr_pct > 3.0:
        threshold *= (1 + atr_pct / 20)

    return max(30.0, min(60.0, round(threshold, 1)))


def _count_recent_hits(symbol: str, hours: int = 24) -> int:
    count = db.q_val(
        "SELECT COUNT(*) FROM alerts WHERE symbol=? AND received_at >= datetime('now', ?)",
        (symbol, f"-{hours} hours"),
        default=0,
    )
    return count or 0


def _check_oi_accumulation(symbol: str) -> dict:
    """Check 48h OI accumulation pattern — multiple OI alerts with flat price."""
    alerts_48h = db.q_all("""
        SELECT oi_change_pct, price_change_pct, received_at
        FROM alerts
        WHERE symbol=? AND source='oi_price'
          AND received_at >= datetime('now', '-48 hours')
        ORDER BY received_at
    """, (symbol,))

    if len(alerts_48h) < 2:
        return {"accumulating": False, "count": len(alerts_48h)}

    # Count how many had OI up but price relatively flat
    buildup_count = sum(
        1 for a in alerts_48h
        if (a["oi_change_pct"] or 0) > 5 and abs(a["price_change_pct"] or 0) < 5
    )

    return {
        "accumulating": buildup_count >= 2,
        "count": len(alerts_48h),
        "buildup_count": buildup_count,
    }


def evaluate(alert: Alert) -> dict | None:
    """Evaluate alert — optimized for high frequency, high RR.

    Philosophy: cast wide net (easy entry), strict SL, let TP run.
    """
    reasons = []
    warnings = []
    score = 0
    signal_type = "opportunity"

    # ── Excluded symbols filter ──
    if alert.symbol in EXCLUDED_SYMBOLS:
        return None

    # ── Market cap filter ──
    if alert.market_cap_usd and alert.market_cap_usd > FILTERS["max_mcap"]:
        return None

    # ── OI analysis ──
    oi_needs_confirm = 0  # How many confirmations needed

    if alert.source == "oi_price":
        if not alert.oi_change_pct or alert.oi_change_pct < FILTERS["min_oi_change"]:
            return None

        reasons.append(f"OI +{alert.oi_change_pct}%")

        if alert.oi_change_pct >= FILTERS["max_oi_change"]:
            score += 2
            warnings.append(f"OI>{FILTERS['max_oi_change']}% 可能已主升段")
            oi_needs_confirm = FILTERS["oi_high_confirm_count"]
        else:
            score += 1  # 5-20% sweet spot
            oi_needs_confirm = FILTERS["oi_confirm_count"]

        # MM building position: OI up, price flat
        if alert.price_change_pct is not None and alert.price_change_pct < 3.0:
            reasons.append(f"OI建倉背離(價僅+{alert.price_change_pct}%)")
            score += 3

        if alert.oi_mcap_ratio and alert.oi_mcap_ratio >= FILTERS["min_oi_mcap_ratio"]:
            reasons.append(f"OI/MCap {alert.oi_mcap_ratio}%")
            score += 2

    elif alert.source == "price_surge":
        if alert.price_change_pct and alert.price_change_pct >= FILTERS["min_price_surge"]:
            reasons.append(f"Price +{alert.price_change_pct}%")
            score += 2
        else:
            return None

    elif alert.source == "ru7_mm":
        # Ru7 Bot market maker alert — purely additive confirmation
        ru7_score = alert.ru7_score or 0
        reasons.append(f"Ru7做市商預警 {ru7_score}分")
        if ru7_score >= 8:
            score += 3
        elif ru7_score >= 6:
            score += 2
        else:
            score += 1
        for tag in alert.ru7_tags:
            if tag:
                reasons.append(tag)

    # ── Technical indicators (get early for ATR) ──
    ta = analyze(alert.pair)
    if not ta:
        ta = {}
    ta_4h = analyze(alert.pair, interval="4h")

    vol_ratio = ta.get("volume_ratio")
    atr = ta.get("atr")
    entry_price = ta.get("current_price")
    atr_pct = (atr / entry_price * 100) if atr and entry_price else None

    # ── Don't chase (ATR-aware threshold) ──
    chase_threshold = _calc_chase_threshold(alert.market_cap_usd, alert.oi_mcap_ratio, atr_pct)

    if alert.price_24h_pct and alert.price_24h_pct > chase_threshold:
        warnings.append(f"24h已漲{alert.price_24h_pct}%")
    elif alert.price_change_pct and alert.price_change_pct > chase_threshold:
        warnings.append(f"1h已飆{alert.price_change_pct}%")

    # ── #1: 48h OI accumulation ──
    oi_accum = _check_oi_accumulation(alert.symbol)
    if oi_accum["accumulating"]:
        reasons.append(f"48h OI累積{oi_accum['buildup_count']}次")
        score += 3  # Very valuable — sustained MM activity

    # ── Market metadata ──
    listing_age = get_listing_age_days(alert.pair)
    vol_rank = get_volume_rank(alert.pair)

    if listing_age is not None:
        if listing_age <= FILTERS["recent_listing_days"]:
            reasons.append(f"新幣 {listing_age}天")
            score += 2
        elif listing_age <= 365:
            score += 1

    if vol_rank:
        if vol_rank["percentile"] >= 80:
            reasons.append(f"熱門 Top{100 - int(vol_rank['percentile'])}%")
            score += 2
        elif vol_rank["percentile"] >= 60:
            score += 1

    # ── Social / sentiment ──
    social = get_social_score(alert.symbol, vol_rank)
    for tag in social.get("social_tags", []):
        reasons.append(tag)
    score += social.get("social_score", 0)

    # ── Smart money indicators (purely additive) ──
    smart_money = ta.get("smart_money")
    if smart_money:
        for tag in smart_money.get("tags", []):
            reasons.append(tag)
        score += smart_money.get("score", 0)

    # ── #2: Volume-price structure ──
    if vol_ratio and vol_ratio >= FILTERS["min_volume_ratio"]:
        reasons.append(f"Vol {vol_ratio}x")
        score += 2
        # Volume-price alignment check
        if alert.price_change_pct and alert.price_change_pct > 5 and vol_ratio >= 2.0:
            score += 1  # Strong volume backing the move
    elif vol_ratio and vol_ratio < 0.5:
        warnings.append(f"量能極低 {vol_ratio}x")
    elif vol_ratio and vol_ratio < 1.0:
        warnings.append(f"量比偏低 {vol_ratio}x")

    # ── Pattern-based analysis (RSI + KDJ) ──
    patterns = ta.get("patterns", [])

    bullish = ["RSI底背離", "KDJ底背離", "KDJ金叉", "RSI低檔鈍化", "KDJ低檔鈍化"]
    for p in bullish:
        if p in patterns:
            reasons.append(p)
            score += 2 if "背離" in p else 1

    # KDJ+RSI resonance
    if "KDJ+RSI低位共振" in patterns:
        reasons.append("KDJ+RSI低位共振")
        score += 4
        if signal_type == "dont_chase":
            signal_type = "opportunity"
            warnings = [w for w in warnings if "已漲" not in w and "已飆" not in w]
    elif "KDJ+RSI共振" in patterns:
        reasons.append("KDJ+RSI共振")
        score += 3
        if signal_type == "dont_chase":
            signal_type = "opportunity"  # Resonance overrides chase
            warnings = [w for w in warnings if "已漲" not in w and "已飆" not in w]
    elif "KDJ+RSI高位共振" in patterns:
        warnings.append("KDJ+RSI高位共振")
        score += 1

    if "KDJ+RSI共振下跌" in patterns:
        warnings.append("KDJ+RSI共振下跌")
        score -= 2

    bearish = ["RSI頂背離", "KDJ頂背離", "KDJ死叉"]
    for p in bearish:
        if p in patterns:
            warnings.append(p)
            score -= 1

    if "RSI高檔鈍化" in patterns:
        warnings.append("RSI高檔鈍化")
    if "KDJ高檔鈍化" in patterns:
        warnings.append("KDJ高檔鈍化")
    if "KDJ金叉(高位)" in patterns:
        warnings.append("KDJ金叉(高位)")

    # ── #4: Multi-timeframe confirmation (4h) ──
    if ta_4h:
        patterns_4h = ta_4h.get("patterns", [])
        bullish_4h = [p for p in patterns_4h if "金叉" in p or "底背離" in p or "共振" in p]
        if bullish_4h:
            reasons.append(f"4h: {bullish_4h[0]}")
            score += 2

        bearish_4h = [p for p in patterns_4h if "頂背離" in p or "死叉" in p]
        if bearish_4h:
            warnings.append(f"4h: {bearish_4h[0]}")

    if not reasons:
        return None

    # ── Confirmation gate ──
    if oi_needs_confirm:
        other_reasons = len(reasons) - 1
        if other_reasons < oi_needs_confirm:
            logger.debug(f"{alert.symbol}: OI {alert.oi_change_pct}%, only {other_reasons} confirmations (need {oi_needs_confirm}), skip")
            return None

    # ── Multi-hit in 24h ──
    hit_count = _count_recent_hits(alert.symbol, hours=24)
    if hit_count >= 3:
        reasons.append(f"24h命中{hit_count}次")
        score += 2
    elif hit_count >= 2:
        reasons.append(f"24h命中{hit_count}次")
        score += 1

    # ── Confidence level ──
    # Data: score 6-7 has 65% win rate (best), score 8+ drops to 22-40%
    # High score often = chasing a move that already happened
    score = max(0, score)
    if 5 <= score <= 7:
        confidence = "high"  # Sweet spot: enough confirmation, not chasing
    elif score >= 8:
        confidence = "mid"   # Over-confirmed = likely already moved
        warnings.append(f"多重確認({score}分)留意追高")
    elif score >= 4:
        confidence = "mid"
    else:
        confidence = "low"

    # ── Signal level ──
    strength = len(reasons)
    if strength >= 4:
        level = "strong"
    elif strength >= 2:
        level = "medium"
    else:
        level = "weak"

    # ── TP/SL based on ATR — aggressive RR ──
    tp_sl = {}
    if entry_price and atr:
        tp_sl = calc_tp_sl(entry_price, atr, confidence)

    # ── Auto-add to watchlist for continuous tracking ──
    add_to_watchlist(alert.symbol, alert.pair, source=f"signal_{confidence}")

    return {
        "symbol": alert.symbol,
        "pair": alert.pair,
        "level": level,
        "signal_type": signal_type,
        "confidence": confidence,
        "score": score,
        "chase_threshold": chase_threshold,
        "hit_count": hit_count,
        "reasons": reasons,
        "warnings": warnings,
        "alert": alert.to_dict(),
        "ta": ta,
        "listing_age": listing_age,
        "vol_rank": vol_rank,
        "social": social,
        "oi_accum": oi_accum if alert.source == "oi_price" else None,
        "tp_sl": tp_sl,
    }
