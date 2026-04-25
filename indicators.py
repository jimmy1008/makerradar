"""Technical indicators — RSI, KDJ with pattern detection (divergence, dulling)."""

import requests
import logging

logger = logging.getLogger(__name__)

BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINGX_KLINES = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
MEXC_KLINES = "https://contract.mexc.com/api/v1/contract/kline"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
BINANCE_LS_RATIO = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
BINANCE_TOP_LS = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"

# Interval mapping for each exchange
_INTERVAL_MAP_BINGX = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
_INTERVAL_MAP_MEXC = {"1m": "Min1", "5m": "Min5", "15m": "Min15", "1h": "Min60", "4h": "Hour4", "1d": "Day1"}


def _fetch_binance_klines(symbol: str, interval: str, limit: int) -> list[dict] | None:
    try:
        r = requests.get(BINANCE_KLINES, params={
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }, timeout=5)
        if r.status_code != 200:
            return None
        return [
            {"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5]), "timestamp": k[0]}
            for k in r.json()
        ]
    except Exception:
        return None


def _fetch_bingx_klines(symbol: str, interval: str, limit: int) -> list[dict] | None:
    """BingX perpetual futures klines. Pair format: BTC-USDT"""
    try:
        # Convert BTCUSDT → BTC-USDT
        sym = symbol.upper().replace("USDT", "-USDT")
        bingx_interval = _INTERVAL_MAP_BINGX.get(interval, interval)
        r = requests.get(BINGX_KLINES, params={
            "symbol": sym, "interval": bingx_interval, "limit": limit,
        }, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        klines = data.get("data", [])
        if not klines:
            return None
        return [
            {"open": float(k["open"]), "high": float(k["high"]),
             "low": float(k["low"]), "close": float(k["close"]),
             "volume": float(k.get("volume", 0)), "timestamp": int(k.get("time", 0))}
            for k in klines
        ]
    except Exception:
        return None


def _fetch_mexc_klines(symbol: str, interval: str, limit: int) -> list[dict] | None:
    """MEXC futures klines. Pair format: BTC_USDT"""
    try:
        # Convert BTCUSDT → BTC_USDT
        sym = symbol.upper().replace("USDT", "_USDT")
        mexc_interval = _INTERVAL_MAP_MEXC.get(interval, interval)
        r = requests.get(MEXC_KLINES + f"/{sym}", params={
            "interval": mexc_interval,
        }, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("success"):
            return None
        klines = data.get("data", {}).get("time", [])
        opens = data.get("data", {}).get("open", [])
        highs = data.get("data", {}).get("high", [])
        lows = data.get("data", {}).get("low", [])
        closes = data.get("data", {}).get("close", [])
        vols = data.get("data", {}).get("vol", [])
        if not klines:
            return None
        result = []
        for i in range(min(len(klines), limit)):
            result.append({
                "open": float(opens[i]), "high": float(highs[i]),
                "low": float(lows[i]), "close": float(closes[i]),
                "volume": float(vols[i]) if i < len(vols) else 0,
                "timestamp": int(klines[i]) * 1000,
            })
        return result[-limit:]
    except Exception:
        return None


def fetch_klines(symbol: str, interval: str = "15m", limit: int = 60) -> list[dict] | None:
    """Fetch futures klines — tries Binance first, then BingX, then MEXC.

    This allows tracking small/new coins that may not be on Binance but are on BingX/MEXC.
    """
    # Try Binance first (most liquid)
    data = _fetch_binance_klines(symbol, interval, limit)
    if data and len(data) >= 10:
        return data

    # Fallback: BingX
    data = _fetch_bingx_klines(symbol, interval, limit)
    if data and len(data) >= 10:
        logger.info(f"Using BingX klines for {symbol}")
        return data

    # Fallback: MEXC
    data = _fetch_mexc_klines(symbol, interval, limit)
    if data and len(data) >= 10:
        logger.info(f"Using MEXC klines for {symbol}")
        return data

    logger.warning(f"No klines from any exchange for {symbol}")
    return None


def calc_rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Calculate RSI series from close prices."""
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_values = []
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rsi_values.append(100 - 100 / (1 + avg_gain / avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rsi_values.append(100 - 100 / (1 + avg_gain / avg_loss))

    return rsi_values


def calc_kdj_series(highs: list[float], lows: list[float], closes: list[float],
                    k_period: int = 9) -> list[dict]:
    """Calculate KDJ series. Returns list of {K, D, J}."""
    if len(closes) < k_period:
        return []

    rsv_list = []
    for i in range(k_period - 1, len(closes)):
        window_high = max(highs[i - k_period + 1: i + 1])
        window_low = min(lows[i - k_period + 1: i + 1])
        if window_high == window_low:
            rsv_list.append(50.0)
        else:
            rsv_list.append((closes[i] - window_low) / (window_high - window_low) * 100)

    k_val, d_val = 50.0, 50.0
    series = []
    for rsv in rsv_list:
        k_val = (2 / 3) * k_val + (1 / 3) * rsv
        d_val = (2 / 3) * d_val + (1 / 3) * k_val
        j_val = 3 * k_val - 2 * d_val
        series.append({"K": round(k_val, 2), "D": round(d_val, 2), "J": round(j_val, 2)})

    return series


def calc_volume_ratio(volumes: list[float], window: int = 20) -> float | None:
    """Current volume / average volume of last N bars."""
    if len(volumes) < window + 1:
        return None
    avg = sum(volumes[-window - 1:-1]) / window
    if avg == 0:
        return None
    return volumes[-1] / avg


# ── Pattern detection ──

def _find_peaks(data: list[float], window: int = 3) -> list[int]:
    """Find local peak indices."""
    peaks = []
    for i in range(window, len(data) - window):
        if all(data[i] >= data[i - j] for j in range(1, window + 1)) and \
           all(data[i] >= data[i + j] for j in range(1, window + 1)):
            peaks.append(i)
    return peaks


def _find_troughs(data: list[float], window: int = 3) -> list[int]:
    """Find local trough indices."""
    troughs = []
    for i in range(window, len(data) - window):
        if all(data[i] <= data[i - j] for j in range(1, window + 1)) and \
           all(data[i] <= data[i + j] for j in range(1, window + 1)):
            troughs.append(i)
    return troughs


def detect_rsi_patterns(closes: list[float], rsi_series: list[float]) -> list[str]:
    """Detect RSI patterns: divergence and dulling."""
    patterns = []
    if len(rsi_series) < 10:
        return patterns

    rsi_now = rsi_series[-1]

    # Align: rsi_series corresponds to closes tail
    offset = len(closes) - len(rsi_series)
    aligned_closes = closes[offset:]

    # ── Dulling (鈍化): RSI stays extreme for 5+ bars ──
    lookback = min(8, len(rsi_series))
    recent_rsi = rsi_series[-lookback:]

    overbought_count = sum(1 for r in recent_rsi if r > 75)
    oversold_count = sum(1 for r in recent_rsi if r < 25)

    if overbought_count >= 5:
        patterns.append("RSI高檔鈍化")  # Strong uptrend, not necessarily reversal
    if oversold_count >= 5:
        patterns.append("RSI低檔鈍化")  # Strong downtrend

    # ── Divergence (背離) ──
    # Bearish: price making higher highs, RSI making lower highs
    price_peaks = _find_peaks(aligned_closes, 2)
    rsi_peaks = _find_peaks(rsi_series, 2)

    if len(price_peaks) >= 2 and len(rsi_peaks) >= 2:
        pp1, pp2 = price_peaks[-2], price_peaks[-1]
        # Find corresponding RSI peaks near price peaks
        rp1 = min(rsi_peaks, key=lambda x: abs(x - pp1), default=None)
        rp2 = min(rsi_peaks, key=lambda x: abs(x - pp2), default=None)
        if rp1 is not None and rp2 is not None and abs(rp1 - pp1) <= 3 and abs(rp2 - pp2) <= 3:
            if aligned_closes[pp2] > aligned_closes[pp1] and rsi_series[rp2] < rsi_series[rp1]:
                patterns.append("RSI頂背離")  # Bearish divergence

    # Bullish: price making lower lows, RSI making higher lows
    price_troughs = _find_troughs(aligned_closes, 2)
    rsi_troughs = _find_troughs(rsi_series, 2)

    if len(price_troughs) >= 2 and len(rsi_troughs) >= 2:
        pt1, pt2 = price_troughs[-2], price_troughs[-1]
        rt1 = min(rsi_troughs, key=lambda x: abs(x - pt1), default=None)
        rt2 = min(rsi_troughs, key=lambda x: abs(x - pt2), default=None)
        if rt1 is not None and rt2 is not None and abs(rt1 - pt1) <= 3 and abs(rt2 - pt2) <= 3:
            if aligned_closes[pt2] < aligned_closes[pt1] and rsi_series[rt2] > rsi_series[rt1]:
                patterns.append("RSI底背離")  # Bullish divergence

    return patterns


def detect_rsi_kdj_resonance(rsi_series: list[float], kdj_series: list[dict]) -> list[str]:
    """Detect RSI+KDJ resonance — both indicators moving together = strong momentum."""
    patterns = []
    if len(rsi_series) < 5 or len(kdj_series) < 5:
        return patterns

    # Align lengths
    n = min(len(rsi_series), len(kdj_series))
    rsi_tail = rsi_series[-n:]
    kdj_tail = kdj_series[-n:]

    rsi_now = rsi_tail[-1]
    rsi_prev = rsi_tail[-3] if len(rsi_tail) >= 3 else rsi_tail[0]
    k_now = kdj_tail[-1]["K"]
    k_prev = kdj_tail[-3]["K"] if len(kdj_tail) >= 3 else kdj_tail[0]["K"]
    j_now = kdj_tail[-1]["J"]

    rsi_rising = rsi_now - rsi_prev > 10
    kdj_rising = k_now - k_prev > 10

    # ── Bullish resonance: both RSI and KDJ rising together ──
    if rsi_rising and kdj_rising:
        # From low zone = strongest (momentum just starting)
        if rsi_prev < 50 and k_prev < 50:
            patterns.append("KDJ+RSI低位共振")
        # From mid zone = still good
        elif rsi_prev < 65 and k_prev < 65:
            patterns.append("KDJ+RSI共振")
        # From high zone = careful, might be ending
        else:
            patterns.append("KDJ+RSI高位共振")

    # ── Bearish resonance: both falling together ──
    rsi_falling = rsi_prev - rsi_now > 10
    kdj_falling = k_prev - k_now > 10
    if rsi_falling and kdj_falling and rsi_prev > 60:
        patterns.append("KDJ+RSI共振下跌")

    return patterns


def detect_kdj_patterns(closes: list[float], kdj_series: list[dict]) -> list[str]:
    """Detect KDJ patterns: cross, divergence, dulling."""
    patterns = []
    if len(kdj_series) < 5:
        return patterns

    offset = len(closes) - len(kdj_series)
    aligned_closes = closes[offset:]

    k_series = [x["K"] for x in kdj_series]
    d_series = [x["D"] for x in kdj_series]
    j_series = [x["J"] for x in kdj_series]

    # ── Golden cross / Death cross ──
    if len(kdj_series) >= 2:
        prev, curr = kdj_series[-2], kdj_series[-1]
        if prev["K"] <= prev["D"] and curr["K"] > curr["D"]:
            if curr["K"] < 50:
                patterns.append("KDJ金叉")  # Bullish cross in low zone = stronger
            else:
                patterns.append("KDJ金叉(高位)")
        elif prev["K"] >= prev["D"] and curr["K"] < curr["D"]:
            if curr["K"] > 50:
                patterns.append("KDJ死叉")
            else:
                patterns.append("KDJ死叉(低位)")

    # ── J-line dulling (鈍化) ──
    lookback = min(8, len(j_series))
    recent_j = j_series[-lookback:]

    j_high_count = sum(1 for j in recent_j if j > 80)
    j_low_count = sum(1 for j in recent_j if j < 20)

    if j_high_count >= 5:
        patterns.append("KDJ高檔鈍化")
    if j_low_count >= 5:
        patterns.append("KDJ低檔鈍化")

    # ── KDJ divergence ──
    price_peaks = _find_peaks(aligned_closes, 2)
    j_peaks = _find_peaks(j_series, 2)

    if len(price_peaks) >= 2 and len(j_peaks) >= 2:
        pp1, pp2 = price_peaks[-2], price_peaks[-1]
        jp1 = min(j_peaks, key=lambda x: abs(x - pp1), default=None)
        jp2 = min(j_peaks, key=lambda x: abs(x - pp2), default=None)
        if jp1 is not None and jp2 is not None and abs(jp1 - pp1) <= 3 and abs(jp2 - pp2) <= 3:
            if aligned_closes[pp2] > aligned_closes[pp1] and j_series[jp2] < j_series[jp1]:
                patterns.append("KDJ頂背離")

    price_troughs = _find_troughs(aligned_closes, 2)
    j_troughs = _find_troughs(j_series, 2)

    if len(price_troughs) >= 2 and len(j_troughs) >= 2:
        pt1, pt2 = price_troughs[-2], price_troughs[-1]
        jt1 = min(j_troughs, key=lambda x: abs(x - pt1), default=None)
        jt2 = min(j_troughs, key=lambda x: abs(x - pt2), default=None)
        if jt1 is not None and jt2 is not None and abs(jt1 - pt1) <= 3 and abs(jt2 - pt2) <= 3:
            if aligned_closes[pt2] < aligned_closes[pt1] and j_series[jt2] > j_series[jt1]:
                patterns.append("KDJ底背離")

    return patterns


# ── Hyperliquid cache ──
_hype_cache: dict = {}
_hype_cache_ts: float = 0
_HYPE_TTL = 60  # 1 min


def _refresh_hype():
    global _hype_cache, _hype_cache_ts
    import time
    if time.time() - _hype_cache_ts < _HYPE_TTL and _hype_cache:
        return
    try:
        r = requests.post(HYPERLIQUID_INFO, json={"type": "metaAndAssetCtxs"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            universe = data[0]["universe"]
            ctxs = data[1]
            _hype_cache = {}
            for coin, ctx in zip(universe, ctxs):
                name = coin["name"].upper()
                mark = float(ctx.get("markPx", 0))
                oi = float(ctx.get("openInterest", 0))
                funding = float(ctx.get("funding", 0))
                _hype_cache[name] = {
                    "oi": oi,
                    "oi_usd": oi * mark,
                    "funding": funding,
                    "mark": mark,
                }
            _hype_cache_ts = time.time()
    except Exception as e:
        logger.error(f"Hyperliquid fetch: {e}")


def fetch_hype_data(symbol: str) -> dict | None:
    """Get Hyperliquid OI + funding for a symbol. Free API, no key."""
    _refresh_hype()
    sym = symbol.upper().replace("USDT", "")
    data = _hype_cache.get(sym)
    if not data:
        return None
    return {
        "oi_usd": data["oi_usd"],
        "funding": data["funding"],
        "mark": data["mark"],
        "on_hype": True,
    }


def fetch_smart_money(symbol: str) -> dict | None:
    """Fetch funding rate, long/short ratio, top trader ratio.

    Returns scoring tags — purely additive, never filters out signals.
    """
    try:
        sym = symbol.upper()
        result = {"tags": [], "score": 0}

        # 1. Funding rate
        r1 = requests.get(BINANCE_FUNDING, params={"symbol": sym, "limit": 1}, timeout=5)
        if r1.status_code == 200 and r1.json():
            fr = float(r1.json()[0]["fundingRate"])
            result["funding_rate"] = round(fr * 100, 4)
            if fr < -0.01:
                result["tags"].append(f"負費率{result['funding_rate']}%")
                result["score"] += 2  # Strong: shorts paying longs
            elif fr < 0:
                result["tags"].append(f"微負費率")
                result["score"] += 1

        # 2. Global long/short account ratio
        r2 = requests.get(BINANCE_LS_RATIO, params={"symbol": sym, "period": "1h", "limit": 1}, timeout=5)
        if r2.status_code == 200 and r2.json():
            ls = float(r2.json()[0]["longShortRatio"])
            result["ls_ratio"] = round(ls, 2)
            if ls < 0.8:
                result["tags"].append(f"散戶偏空({ls})")
                result["score"] += 2  # Retail shorting = squeeze potential
            elif ls < 1.0:
                result["tags"].append(f"散戶微空({ls})")
                result["score"] += 1

        # 3. Top trader position ratio
        r3 = requests.get(BINANCE_TOP_LS, params={"symbol": sym, "period": "1h", "limit": 1}, timeout=5)
        if r3.status_code == 200 and r3.json():
            d = r3.json()[0]
            top_ls = float(d["longShortRatio"])
            result["top_ls_ratio"] = round(top_ls, 2)
            if top_ls > 1.5:
                result["tags"].append(f"大戶偏多({top_ls})")
                result["score"] += 2
            elif top_ls > 1.2:
                result["tags"].append(f"大戶微多({top_ls})")
                result["score"] += 1

        # 4. Hyperliquid OI cross-reference
        hype = fetch_hype_data(sym)
        if hype:
            result["hype_oi_usd"] = hype["oi_usd"]
            result["hype_funding"] = round(hype["funding"] * 100, 4)
            result["on_hype"] = True

            if hype["oi_usd"] > 500_000:
                result["tags"].append(f"Hype OI ${hype['oi_usd']/1e6:.1f}M")
                result["score"] += 2  # Present on both exchanges = more serious
            elif hype["oi_usd"] > 100_000:
                result["tags"].append(f"Hype OI ${hype['oi_usd']/1e3:.0f}K")
                result["score"] += 1

            if hype["funding"] < -0.01:
                result["tags"].append(f"Hype負費率{result['hype_funding']}%")
                result["score"] += 1
        else:
            result["on_hype"] = False

        return result
    except Exception as e:
        logger.error(f"Smart money {symbol}: {e}")
        return None


def calc_atr(highs: list[float], lows: list[float], closes: list[float],
             period: int = 14) -> float | None:
    """Calculate Average True Range."""
    if len(closes) < period + 1:
        return None
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
    if len(tr_list) < period:
        return None
    atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
    return atr


# ── Swing point & structure detection ──


def find_swing_points(highs: list[float], lows: list[float],
                      window: int = 3) -> dict:
    """Find swing highs and swing lows from OHLC data.

    Returns {swing_highs: [(index, price)], swing_lows: [(index, price)]}
    """
    swing_highs = []
    swing_lows = []

    for i in range(window, len(highs) - window):
        # Swing high: highest in local window
        if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
            swing_highs.append((i, highs[i]))

        # Swing low: lowest in local window
        if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
            swing_lows.append((i, lows[i]))

    return {"swing_highs": swing_highs, "swing_lows": swing_lows}


def detect_trend(closes: list[float], highs: list[float], lows: list[float],
                 window: int = 3) -> dict:
    """Detect trend using swing structure.

    Downtrend: lower highs + lower lows
    Uptrend: higher highs + higher lows
    Ranging: mixed
    """
    swings = find_swing_points(highs, lows, window)
    sh = swings["swing_highs"]
    sl = swings["swing_lows"]

    trend = "ranging"
    strength = 0  # How many consecutive structure confirmations

    # Need at least 2 swing highs and 2 swing lows
    if len(sh) >= 2 and len(sl) >= 2:
        # Check last 3 pairs for consistency
        n = min(3, len(sh), len(sl))
        recent_sh = sh[-n:]
        recent_sl = sl[-n:]

        lower_highs = sum(1 for i in range(1, len(recent_sh))
                          if recent_sh[i][1] < recent_sh[i - 1][1])
        lower_lows = sum(1 for i in range(1, len(recent_sl))
                         if recent_sl[i][1] < recent_sl[i - 1][1])
        higher_highs = sum(1 for i in range(1, len(recent_sh))
                           if recent_sh[i][1] > recent_sh[i - 1][1])
        higher_lows = sum(1 for i in range(1, len(recent_sl))
                          if recent_sl[i][1] > recent_sl[i - 1][1])

        pairs = n - 1
        if lower_highs >= pairs and lower_lows >= pairs:
            trend = "downtrend"
            strength = lower_highs + lower_lows
        elif higher_highs >= pairs and higher_lows >= pairs:
            trend = "uptrend"
            strength = higher_highs + higher_lows
        elif lower_lows >= pairs:
            trend = "downtrend"
            strength = lower_lows

    # Price relative to structure
    cur = closes[-1]
    last_sl_price = sl[-1][1] if sl else None
    last_sh_price = sh[-1][1] if sh else None

    return {
        "trend": trend,
        "strength": strength,
        "swing_highs": sh,
        "swing_lows": sl,
        "last_swing_low": last_sl_price,
        "last_swing_high": last_sh_price,
        "above_structure": cur > last_sl_price if last_sl_price else None,
    }


def detect_momentum_decay(closes: list[float], highs: list[float],
                          lows: list[float], volumes: list[float],
                          lookback: int = 8) -> dict:
    """Detect 「跌勢漸緩」— momentum is fading before a reversal.

    Checks:
    1. Candle body shrinking (recent candles smaller than earlier ones)
    2. ATR decreasing (volatility contracting)
    3. Volume drying up then sudden spike
    """
    if len(closes) < lookback + 3:
        return {"decaying": False, "signals": [], "score": 0}

    signals = []
    score = 0

    # Split into earlier half vs recent half
    mid = len(closes) - lookback
    early_bodies = [abs(closes[i] - closes[i - 1]) for i in range(mid - lookback, mid) if i > 0]
    recent_bodies = [abs(closes[i] - closes[i - 1]) for i in range(mid, len(closes))]

    if early_bodies and recent_bodies:
        avg_early = sum(early_bodies) / len(early_bodies)
        avg_recent = sum(recent_bodies) / len(recent_bodies)
        if avg_early > 0 and avg_recent / avg_early < 0.5:
            signals.append("K線實體縮小")
            score += 2
        elif avg_early > 0 and avg_recent / avg_early < 0.7:
            signals.append("動能減弱")
            score += 1

    # ATR contraction: recent ATR < earlier ATR
    if len(highs) >= lookback * 2:
        early_ranges = [highs[i] - lows[i] for i in range(mid - lookback, mid)]
        recent_ranges = [highs[i] - lows[i] for i in range(mid, len(highs))]
        avg_early_r = sum(early_ranges) / len(early_ranges)
        avg_recent_r = sum(recent_ranges) / len(recent_ranges)
        if avg_early_r > 0 and avg_recent_r / avg_early_r < 0.5:
            signals.append("波動收斂")
            score += 2
        elif avg_early_r > 0 and avg_recent_r / avg_early_r < 0.7:
            signals.append("ATR縮小")
            score += 1

    # Volume dry-up: recent avg volume much lower than earlier
    if len(volumes) >= lookback * 2:
        early_vol = sum(volumes[mid - lookback:mid]) / lookback
        recent_vol = sum(volumes[mid:-1]) / max(len(volumes[mid:-1]), 1)  # Exclude last bar
        if early_vol > 0 and recent_vol / early_vol < 0.4:
            signals.append("量能枯竭")
            score += 2

    return {
        "decaying": score >= 2,
        "signals": signals,
        "score": score,
    }


def detect_reversal(closes: list[float], highs: list[float], lows: list[float],
                    volumes: list[float], trend_info: dict) -> dict:
    """Detect reversal from downtrend — 「跌久了突然不一樣」.

    Core pattern (from user's FRAX trade):
    1. Sustained downtrend with momentum decay (candles shrinking)
    2. Suddenly a strong bullish candle with volume
    3. Entry on candle close confirmation

    Conditions:
    1. Momentum decay (K線縮小 + 波動收斂 + 量能枯竭)
    2. Bullish candle: strong body, volume spike
    3. Break above last swing high (structure break)
    4. Higher low forming after lower lows
    """
    if len(closes) < 10:
        return {"reversal": False, "signals": [], "score": 0}

    signals = []
    score = 0
    cur_close = closes[-1]
    cur_open = closes[-2] if len(closes) >= 2 else cur_close
    cur_high = highs[-1]
    cur_low = lows[-1]

    is_downtrend = trend_info["trend"] == "downtrend"
    is_ranging = trend_info["trend"] == "ranging"

    # ── 0. Momentum decay (跌勢漸緩) ──
    decay = detect_momentum_decay(closes, highs, lows, volumes)
    if decay["decaying"]:
        for s in decay["signals"]:
            signals.append(s)
        score += decay["score"]

    # ── 1. Volume spike on current candle ──
    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    cur_vol = volumes[-1]
    vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0

    if vol_ratio >= 3.0:
        signals.append(f"放量{vol_ratio:.1f}x")
        score += 3
    elif vol_ratio >= 2.0:
        signals.append(f"爆量{vol_ratio:.1f}x")
        score += 2

    # ── 2. Bullish candle structure (「不一樣的K」) ──
    body = cur_close - cur_open
    candle_range = cur_high - cur_low
    if candle_range > 0 and body > 0:
        body_ratio = body / candle_range

        # Compare this candle's body to recent average body
        recent_bodies = [abs(closes[i] - closes[i - 1]) for i in range(max(1, len(closes) - 6), len(closes) - 1)]
        avg_body = sum(recent_bodies) / len(recent_bodies) if recent_bodies else 0

        if avg_body > 0 and body > avg_body * 2.0:
            signals.append(f"陽線突破({body / avg_body:.1f}x均幅)")
            score += 3
        elif body_ratio >= 0.6:
            signals.append("陽線實體強")
            score += 1

        # Long lower wick = buying pressure
        lower_wick = (min(cur_close, cur_open) - cur_low) / candle_range
        if lower_wick >= 0.4:
            signals.append("下影線長")
            score += 1

    # ── 3. Break above last swing high (structure break) ──
    last_sh = trend_info.get("last_swing_high")
    if last_sh and cur_close > last_sh and (is_downtrend or is_ranging):
        signals.append("突破前高結構")
        score += 3

    # ── 4. Higher low forming ──
    swing_lows = trend_info.get("swing_lows", [])
    if len(swing_lows) >= 2:
        prev_low = swing_lows[-2][1]
        last_low = swing_lows[-1][1]
        if last_low > prev_low and (is_downtrend or is_ranging):
            signals.append("形成更高低點")
            score += 2

    # Reversal = downtrend (or ranging after downtrend) + enough signals
    # With momentum decay: lower threshold (decay itself is a signal)
    min_score = 3 if decay["decaying"] else 4
    is_reversal = (is_downtrend or is_ranging) and score >= min_score

    return {
        "reversal": is_reversal,
        "signals": signals,
        "score": score,
        "vol_ratio": round(vol_ratio, 1),
        "is_downtrend": is_downtrend,
        "momentum_decay": decay,
    }


def calc_structure_sl(lows: list[float], swing_lows: list[tuple],
                      current_price: float) -> dict | None:
    """Calculate stop loss based on 4H structure (swing low).

    Strategy: SL = below the most recent swing low.
    For new coins that dumped then reversed, this is the dump bottom.
    No max distance cap — if the user enters a reversal trade,
    the SL IS the bottom regardless of distance (that's the structure).
    """
    if not swing_lows:
        # No swing points yet — use lowest of recent candles
        if not lows:
            return None
        sl_price = min(lows[-10:]) if len(lows) >= 10 else min(lows)
    else:
        # Find the most recent swing low that's BELOW current price
        valid = [p for _, p in swing_lows if p < current_price]
        if valid:
            sl_price = valid[-1]  # Most recent one below price
        else:
            # All swing lows above price — use lowest of recent candles
            sl_price = min(lows[-10:]) if len(lows) >= 10 else min(lows)

    # Buffer: 0.3% below swing low to avoid exact-level sweeps
    buffer = sl_price * 0.003
    sl = sl_price - buffer
    sl_pct = (current_price - sl) / current_price * 100

    return {
        "sl": round(sl, 6),
        "sl_pct": round(sl_pct, 2),
        "structure_level": round(sl_price, 6),
    }


def calc_multi_tp(closes: list[float], highs: list[float],
                  swing_highs: list[tuple], swing_lows: list[tuple],
                  current_price: float, atr: float | None) -> dict:
    """Calculate multiple TP options for the trader to choose from.

    1. Fib extensions (0.618, 1.0, 1.618) from last swing
    2. Previous high (resistance)
    3. ATR trailing reference
    4. All-time / period high
    """
    tp_options = {}

    # 1. Fibonacci extensions from most recent swing low → swing high
    if swing_lows and swing_highs:
        # Find the most recent low that's below current price
        valid_lows = [(i, p) for i, p in swing_lows if p < current_price]
        if valid_lows:
            last_low_idx, last_low = valid_lows[-1]
            # Find swing high after this swing low
            relevant_highs = [(i, p) for i, p in swing_highs if i > last_low_idx]
            if relevant_highs:
                _, swing_high = relevant_highs[0]
            else:
                swing_high = max(p for _, p in swing_highs)

            swing_range = swing_high - last_low
            if swing_range > 0:
                fib_1618 = last_low + swing_range * 1.618
                fib_2618 = last_low + swing_range * 2.618

                # Only show Fibs above current price
                if fib_1618 > current_price:
                    tp_options["fib_1618"] = {
                        "price": round(fib_1618, 6),
                        "pct": round((fib_1618 - current_price) / current_price * 100, 2),
                        "label": "Fib 1.618",
                    }
                if swing_high > current_price:
                    tp_options["fib_1000"] = {
                        "price": round(swing_high, 6),
                        "pct": round((swing_high - current_price) / current_price * 100, 2),
                        "label": "前波段高點",
                    }
                if fib_2618 > current_price:
                    tp_options["fib_2618"] = {
                        "price": round(fib_2618, 6),
                        "pct": round((fib_2618 - current_price) / current_price * 100, 2),
                        "label": "Fib 2.618",
                    }

    # 2. Previous significant high (resistance)
    if swing_highs:
        # Highest swing high in the dataset
        max_sh = max(swing_highs, key=lambda x: x[1])
        if max_sh[1] > current_price:
            tp_options["prev_high"] = {
                "price": round(max_sh[1], 6),
                "pct": round((max_sh[1] - current_price) / current_price * 100, 2),
                "label": "前高",
            }

    # 3. Period high (absolute highest in data)
    period_high = max(highs)
    if period_high > current_price * 1.01:  # At least 1% above
        tp_options["period_high"] = {
            "price": round(period_high, 6),
            "pct": round((period_high - current_price) / current_price * 100, 2),
            "label": "區間最高",
        }

    # 4. ATR trailing reference
    if atr:
        trail_price = current_price + atr * 3
        tp_options["atr_trail"] = {
            "price": round(trail_price, 6),
            "pct": round(atr * 3 / current_price * 100, 2),
            "label": f"ATR×3 移動止盈",
            "trail_atr": round(atr * 2, 6),
            "trail_note": "回落2×ATR平倉",
        }

    return tp_options


def analyze_for_entry(symbol: str) -> dict | None:
    """Full analysis for watchlist entry detection — uses 4H timeframe.

    Returns trend, reversal signals, structure SL, and multi-option TP.
    """
    klines = fetch_klines(symbol, "4h", limit=100)
    if not klines or len(klines) < 30:
        return None

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    volumes = [k["volume"] for k in klines]

    # Trend detection
    trend_info = detect_trend(closes, highs, lows, window=3)

    # Reversal detection (bottom reversal pattern)
    reversal = detect_reversal(closes, highs, lows, volumes, trend_info)

    # Pullback detection (trend pullback pattern)
    pullback = detect_pullback(closes, highs, lows, volumes, trend_info)

    # Accumulation detection (volume up, price flat)
    accumulation = detect_accumulation(closes, highs, lows, volumes)

    # RSI/KDJ for confluence
    rsi_series = calc_rsi_series(closes)
    kdj_series = calc_kdj_series(highs, lows, closes)
    rsi = rsi_series[-1] if rsi_series else None
    kdj = kdj_series[-1] if kdj_series else None

    # RSI oversold adds to reversal score
    if rsi and rsi < 35:
        reversal["signals"].append(f"RSI超賣({rsi:.0f})")
        reversal["score"] += 2
        reversal["reversal"] = reversal["reversal"] or (reversal["score"] >= 3 and trend_info["trend"] == "downtrend")

    # KDJ golden cross in low zone
    if kdj_series and len(kdj_series) >= 2:
        prev_kdj = kdj_series[-2]
        if prev_kdj["K"] <= prev_kdj["D"] and kdj["K"] > kdj["D"] and kdj["K"] < 50:
            reversal["signals"].append("KDJ低位金叉")
            reversal["score"] += 2
            reversal["reversal"] = reversal["reversal"] or (reversal["score"] >= 3 and trend_info["trend"] == "downtrend")

    cur_price = closes[-1]
    atr = calc_atr(highs, lows, closes)

    # Structure-based SL (4H swing low)
    structure_sl = calc_structure_sl(lows, trend_info["swing_lows"], cur_price)

    # Multi-option TP
    tp_options = calc_multi_tp(closes, highs,
                               trend_info["swing_highs"],
                               trend_info["swing_lows"],
                               cur_price, atr)

    # MFE reference: max favorable from current price
    mfe_data = None
    if atr:
        atr_pct = atr / cur_price * 100
        mfe_data = {"atr_pct": round(atr_pct, 2), "atr": round(atr, 6)}

    # RSI adds to pullback if not overbought
    if rsi and rsi < 60 and pullback["pullback"]:
        pullback["signals"].append(f"RSI有空間({rsi:.0f})")
        pullback["score"] += 1

    # Determine entry type and score — pick the strongest signal
    candidates = []
    if reversal["reversal"]:
        candidates.append(("reversal", reversal["score"]))
    if pullback["pullback"]:
        candidates.append(("pullback", pullback["score"]))
    if accumulation["accumulating"]:
        candidates.append(("accumulation", accumulation["score"]))

    if candidates:
        entry_type, entry_score = max(candidates, key=lambda x: x[1])
    else:
        entry_type = None
        entry_score = 0

    # ── RR gate: reject if risk/reward < 1.5 ──
    if entry_type and structure_sl and tp_options:
        sl_pct = structure_sl.get("sl_pct", 0)
        # Find best realistic TP (closest above current price)
        best_tp_pct = 0
        for v in tp_options.values():
            tp_pct = v.get("pct", 0)
            if tp_pct > 0 and (best_tp_pct == 0 or tp_pct < best_tp_pct):
                best_tp_pct = tp_pct
        if sl_pct > 0 and best_tp_pct > 0:
            rr = best_tp_pct / sl_pct
            if rr < 3.0:
                entry_type = None
                entry_score = 0

    return {
        "symbol": symbol,
        "current_price": cur_price,
        "trend": trend_info,
        "reversal": reversal,
        "pullback": pullback,
        "accumulation": accumulation,
        "entry_type": entry_type,
        "entry_score": entry_score,
        "rsi": round(rsi, 2) if rsi else None,
        "kdj": kdj,
        "atr": round(atr, 6) if atr else None,
        "structure_sl": structure_sl,
        "tp_options": tp_options,
        "mfe": mfe_data,
        "klines_count": len(klines),
    }


def detect_accumulation(closes: list[float], highs: list[float], lows: list[float],
                        volumes: list[float], lookback: int = 10) -> dict:
    """Detect volume accumulation with flat price — 「量增價平」(吸籌).

    Smart money building positions without moving price.
    Conditions:
    1. Volume trending up over recent bars
    2. Price range stays tight (low ATR / small candles)
    3. Bonus: volume spike on latest bar while price still flat
    """
    if len(closes) < lookback + 5:
        return {"accumulating": False, "signals": [], "score": 0}

    signals = []
    score = 0

    # Split volume into earlier vs recent
    mid = len(volumes) - lookback
    early_vol = volumes[mid - lookback:mid]
    recent_vol = volumes[mid:]

    avg_early_vol = sum(early_vol) / len(early_vol) if early_vol else 1
    avg_recent_vol = sum(recent_vol) / len(recent_vol) if recent_vol else 1

    # ── 1. Volume increasing ──
    vol_growth = avg_recent_vol / avg_early_vol if avg_early_vol > 0 else 0
    if vol_growth >= 2.0:
        signals.append(f"量能增加{vol_growth:.1f}x")
        score += 3
    elif vol_growth >= 1.5:
        signals.append(f"量能漸增{vol_growth:.1f}x")
        score += 2

    # ── 2. Price staying flat ──
    recent_closes = closes[-lookback:]
    price_range = max(recent_closes) - min(recent_closes)
    avg_price = sum(recent_closes) / len(recent_closes)
    range_pct = (price_range / avg_price * 100) if avg_price > 0 else 999

    if range_pct < 3:
        signals.append(f"價格區間僅{range_pct:.1f}%")
        score += 3
    elif range_pct < 5:
        signals.append(f"價格相對平穩({range_pct:.1f}%)")
        score += 2
    elif range_pct < 8:
        signals.append(f"價格波動尚可({range_pct:.1f}%)")
        score += 1

    # ── 3. Candle bodies shrinking (consolidation) ──
    recent_bodies = [abs(closes[i] - closes[i - 1]) for i in range(mid, len(closes))]
    early_bodies = [abs(closes[i] - closes[i - 1]) for i in range(mid - lookback, mid) if i > 0]
    if early_bodies and recent_bodies:
        avg_eb = sum(early_bodies) / len(early_bodies)
        avg_rb = sum(recent_bodies) / len(recent_bodies)
        if avg_eb > 0 and avg_rb / avg_eb < 0.5:
            signals.append("K線實體收窄")
            score += 1

    # ── 4. Latest bar volume spike (ready to break out) ──
    if avg_recent_vol > 0 and volumes[-1] / avg_recent_vol >= 1.5:
        signals.append(f"最新K量突增{volumes[-1] / avg_recent_vol:.1f}x")
        score += 2

    # ── 5. Bullish breakout candle on top of accumulation ──
    if len(closes) >= 2:
        body = closes[-1] - closes[-2]
        if body > 0 and price_range > 0 and body / avg_price * 100 > range_pct * 0.5:
            signals.append("突破盤整區間")
            score += 2

    is_accumulating = score >= 5 and vol_growth >= 1.5 and range_pct < 10

    return {
        "accumulating": is_accumulating,
        "signals": signals,
        "score": score,
        "vol_growth": round(vol_growth, 2),
        "range_pct": round(range_pct, 2),
    }


def detect_pullback(closes: list[float], highs: list[float], lows: list[float],
                    volumes: list[float], trend_info: dict) -> dict:
    """Detect trend pullback entry — 「漲一波後回踩支撐」(RIVER pattern).

    Conditions:
    1. Uptrend established (higher highs + higher lows)
    2. Currently pulling back (price dropped from recent high)
    3. Approaching support (Fib 0.5-0.618 or swing low)
    4. Showing bounce confirmation (bullish candle)
    """
    if len(closes) < 15:
        return {"pullback": False, "signals": [], "score": 0}

    signals = []
    score = 0
    cur_close = closes[-1]
    cur_open = closes[-2] if len(closes) >= 2 else cur_close
    cur_high = highs[-1]
    cur_low = lows[-1]

    is_uptrend = trend_info["trend"] == "uptrend"
    swing_highs = trend_info.get("swing_highs", [])
    swing_lows = trend_info.get("swing_lows", [])

    if not is_uptrend or not swing_highs or not swing_lows:
        return {"pullback": False, "signals": [], "score": 0}

    # Recent swing high = the peak before pullback
    recent_high = max(p for _, p in swing_highs[-3:])
    # Recent swing low = the support level
    recent_low = swing_lows[-1][1]

    # ── 1. Currently pulling back from high ──
    drop_pct = (recent_high - cur_close) / recent_high * 100
    if drop_pct < 5:
        # Not pulling back enough
        return {"pullback": False, "signals": [], "score": 0}

    signals.append(f"回踩{drop_pct:.1f}%")
    score += 1

    # ── 2. Fibonacci level check ──
    swing_range = recent_high - recent_low
    if swing_range > 0:
        fib_level = (recent_high - cur_close) / swing_range
        if 0.45 <= fib_level <= 0.7:
            signals.append(f"Fib {fib_level:.3f}")
            score += 3  # Sweet spot
        elif 0.3 <= fib_level <= 0.8:
            signals.append(f"Fib {fib_level:.3f}")
            score += 1
    else:
        fib_level = None

    # ── 3. Near swing low support ──
    dist_to_support = (cur_close - recent_low) / cur_close * 100
    if dist_to_support < 5:
        signals.append(f"接近支撐位")
        score += 2

    # ── 4. Bounce confirmation (bullish candle) ──
    body = cur_close - cur_open
    candle_range = cur_high - cur_low
    if candle_range > 0 and body > 0:
        body_ratio = body / candle_range
        if body_ratio >= 0.5:
            signals.append("反彈陽線")
            score += 2

        # Volume on bounce
        avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        if volumes[-1] > avg_vol * 1.5:
            signals.append(f"反彈放量{volumes[-1] / avg_vol:.1f}x")
            score += 2

    # ── 5. RSI not overbought (room to run) ──
    # Checked externally by caller

    # ── 6. Higher low maintained ──
    if len(swing_lows) >= 2:
        if swing_lows[-1][1] > swing_lows[-2][1]:
            signals.append("維持更高低點")
            score += 2

    is_pullback = score >= 5

    return {
        "pullback": is_pullback,
        "signals": signals,
        "score": score,
        "drop_pct": round(drop_pct, 2),
        "fib_level": round(fib_level, 3) if fib_level else None,
        "support_price": recent_low,
        "resistance_price": recent_high,
    }


MIN_SL_PCT = 1.5   # Minimum SL distance — prevents noise wicks from sweeping
MIN_TP1_PCT = 2.0  # Minimum TP1 distance — ensures meaningful target


def calc_tp_sl(entry: float, atr: float, confidence: str = "mid") -> dict:
    """Calculate multi-level TP/SL based on ATR with minimum floor.

    SL floor: 1.5% minimum — if ATR-based SL is tighter, widen to floor.
    TP floor: 2.0% minimum — keeps RR meaningful.

    Confidence adjusts multipliers:
      high: SL=1.2x, TP=2.0/4.0/7.0x, trail=2.5x
      mid:  SL=1.2x, TP=1.5/3.0/5.0x, trail=2.0x
      low:  SL=1.0x, TP=1.5/2.5/4.0x, trail=1.5x
    """
    # TP1 set further out — data shows 44/45 TPs are TP1, profits cut too short
    # User targets 3R+ trades, so TP1 should be meaningful
    if confidence == "high":
        sl_m = 1.2
        tp_m = [3.0, 5.0, 8.0]
        trail_m = 2.5
    elif confidence == "mid":
        sl_m = 1.2
        tp_m = [2.5, 4.0, 6.0]
        trail_m = 2.0
    else:
        sl_m = 1.0
        tp_m = [2.0, 3.5, 5.0]
        trail_m = 1.5

    # Calculate ATR-based distances
    sl_dist = atr * sl_m
    tp1_dist = atr * tp_m[0]
    tp2_dist = atr * tp_m[1]
    tp3_dist = atr * tp_m[2]
    trail_dist = atr * trail_m

    # Apply minimum floor — scale up all levels proportionally if ATR too small
    sl_pct = sl_dist / entry * 100
    if sl_pct < MIN_SL_PCT:
        scale = MIN_SL_PCT / sl_pct
        sl_dist *= scale
        tp1_dist *= scale
        tp2_dist *= scale
        tp3_dist *= scale
        trail_dist *= scale

    tp1_pct = tp1_dist / entry * 100
    if tp1_pct < MIN_TP1_PCT:
        tp_scale = MIN_TP1_PCT / tp1_pct
        tp1_dist *= tp_scale
        tp2_dist *= tp_scale
        tp3_dist *= tp_scale
        trail_dist *= tp_scale

    sl = entry - sl_dist
    tp1 = entry + tp1_dist
    tp2 = entry + tp2_dist
    tp3 = entry + tp3_dist

    # Recalculate actual percentages
    actual_sl_pct = sl_dist / entry * 100
    actual_tp1_pct = tp1_dist / entry * 100

    # Overall RR based on weighted average TP vs SL
    avg_tp_dist = 0.3 * tp1_dist + 0.3 * tp2_dist + 0.2 * tp3_dist + 0.2 * tp3_dist
    rr = round(avg_tp_dist / sl_dist, 1) if sl_dist > 0 else 0

    return {
        "sl": round(sl, 6),
        "sl_pct": round(actual_sl_pct, 2),
        "tp1": round(tp1, 6),
        "tp1_pct": round(tp1_dist / entry * 100, 2),
        "tp1_size": 30,
        "tp2": round(tp2, 6),
        "tp2_pct": round(tp2_dist / entry * 100, 2),
        "tp2_size": 30,
        "tp3": round(tp3, 6),
        "tp3_pct": round(tp3_dist / entry * 100, 2),
        "tp3_size": 20,
        "tp4_trail": round(trail_dist, 6),
        "tp4_trail_pct": round(trail_dist / entry * 100, 2),
        "tp4_size": 20,
        "rr": rr,
        "atr": round(atr, 6),
    }


def analyze(symbol: str, interval: str = "15m") -> dict | None:
    """Full analysis with pattern detection."""
    klines = fetch_klines(symbol, interval, limit=60)
    if not klines or len(klines) < 20:
        return None

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    volumes = [k["volume"] for k in klines]

    rsi_series = calc_rsi_series(closes)
    kdj_series = calc_kdj_series(highs, lows, closes)
    vol_ratio = calc_volume_ratio(volumes)

    rsi = rsi_series[-1] if rsi_series else None
    kdj = kdj_series[-1] if kdj_series else None

    # Pattern detection
    rsi_patterns = detect_rsi_patterns(closes, rsi_series)
    kdj_patterns = detect_kdj_patterns(closes, kdj_series)
    resonance = detect_rsi_kdj_resonance(rsi_series, kdj_series)

    cur_price = closes[-1]
    price_change = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0

    atr = calc_atr(highs, lows, closes)

    # On-chain sentiment
    smart_money = fetch_smart_money(symbol)

    return {
        "smart_money": smart_money,
        "rsi": round(rsi, 2) if rsi else None,
        "kdj": kdj,
        "volume_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "atr": round(atr, 6) if atr else None,
        "current_price": cur_price,
        "price_change_pct": round(price_change, 2),
        "klines_count": len(klines),
        "rsi_patterns": rsi_patterns,
        "kdj_patterns": kdj_patterns,
        "resonance": resonance,
        "patterns": rsi_patterns + kdj_patterns + resonance,
    }
