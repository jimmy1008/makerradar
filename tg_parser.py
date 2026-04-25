"""Parse alert messages from TG monitoring channels."""

import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta

TW = timezone(timedelta(hours=8))


@dataclass
class Alert:
    symbol: str          # e.g. "GIGGLE"
    pair: str            # e.g. "GIGGLEUSDT"
    source: str          # "oi_price", "price_surge", "ru7_mm"
    oi_change_pct: float | None = None
    price_change_pct: float | None = None
    time_window_sec: int | None = None
    oi_usd: float | None = None
    oi_mcap_ratio: float | None = None
    price_24h_pct: float | None = None
    market_cap_usd: float | None = None
    ru7_score: int | None = None
    ru7_tags: list[str] = field(default_factory=list)
    entry_price_raw: float | None = None
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── OI & Price channel ──
_RE_OI_PRICE = re.compile(
    r"(\w+USDT)\s+Binance\s+openinterest\s+([+-]?[\d.]+)%.*?"
    r"Price\s+([+-]?[\d.]+)%.*?"
    r"past\s+(\d+)\s+seconds.*?"
    r"OI:\s*\$([\d.]+)([MBK]?).*?"
    r"OI/Marketcap\s+ratio:\s*([\d.]+)%.*?"
    r"24H\s+Price\s+Change:\s*([+-]?[\d.]+)%",
    re.IGNORECASE | re.DOTALL,
)

_RE_MCAP = re.compile(
    r"(?:MarketCap|\$):\s*\$([\d.]+)([MBK]?)",
    re.IGNORECASE,
)

# ── Price surge channel ──
_RE_SURGE = re.compile(
    r"(\w+USDT)\s+Binance\s+futures\s+price\s+surged?\s+([+-]?[\d.]+)%.*?"
    r"past\s+(\d+)\s+seconds",
    re.IGNORECASE | re.DOTALL,
)

# ── Ru7 Bot 做市商預警 ──
# ↑ WET [多] $0.1197 | 🔥異常放量 📈階梯拉盤 | 9分 | $16.1M
# ↑ BULLA [多] $0.0117 | 🔥異常放量 📈階梯拉盤 | 9分 | $185.1M
_RE_RU7 = re.compile(
    r"[↑↓]\s*(\w+)\s*\[([多空])\]\s*\$([\d.]+)\s*\|(.+?)\|\s*(\d+)分\s*\|\s*\$([\d.]+)([MBK]?)",
    re.DOTALL,
)


def _parse_amount(val: str, unit: str) -> float:
    v = float(val)
    u = unit.upper()
    if u == "B":
        return v * 1e9
    if u == "M":
        return v * 1e6
    if u == "K":
        return v * 1e3
    return v


def _extract_mcap(text: str) -> float | None:
    m = _RE_MCAP.search(text)
    if m:
        return _parse_amount(m.group(1), m.group(2))
    return None


def _strip_symbol(pair: str) -> str:
    return pair.replace("USDT", "").replace("usdt", "")


def parse_message(text: str) -> Alert | None:
    """Try to parse a TG channel message into an Alert.

    OI/Surge channels: only 🟢 bullish.
    Ru7 Bot: only [多] long signals.
    """
    if not text:
        return None

    now = datetime.now(TW).isoformat()

    # ── Try Ru7 Bot format first (doesn't need 🟢) ──
    m = _RE_RU7.search(text)
    if m:
        symbol = m.group(1).upper()
        direction = m.group(2)
        price = float(m.group(3))
        tags_raw = m.group(4).strip()
        score = int(m.group(5))
        mcap = _parse_amount(m.group(6), m.group(7))

        # Only long signals
        if direction != "多":
            return None

        # Parse tags like "🔥異常放量 📈階梯拉盤"
        tags = [t.strip() for t in re.split(r'[🔥📈📉⚡💎🚀\s]+', tags_raw) if t.strip()]

        pair = symbol + "USDT"
        return Alert(
            symbol=symbol,
            pair=pair,
            source="ru7_mm",
            market_cap_usd=mcap,
            ru7_score=score,
            ru7_tags=tags,
            entry_price_raw=price,
            timestamp=now,
        )

    # ── OI & Surge channels need 🟢 ──
    if "\U0001f7e2" not in text:
        return None

    # Try OI & Price format
    m = _RE_OI_PRICE.search(text)
    if m:
        oi_pct = float(m.group(2))
        price_pct = float(m.group(3))
        if oi_pct < 0 or price_pct < 0:
            return None
        pair = m.group(1).upper()
        mcap = _extract_mcap(text)
        return Alert(
            symbol=_strip_symbol(pair),
            pair=pair,
            source="oi_price",
            oi_change_pct=float(m.group(2)),
            price_change_pct=float(m.group(3)),
            time_window_sec=int(m.group(4)),
            oi_usd=_parse_amount(m.group(5), m.group(6)),
            oi_mcap_ratio=float(m.group(7)),
            price_24h_pct=float(m.group(8)),
            market_cap_usd=mcap,
            timestamp=now,
        )

    # Try price surge format
    m = _RE_SURGE.search(text)
    if m:
        pair = m.group(1).upper()
        mcap = _extract_mcap(text)
        return Alert(
            symbol=_strip_symbol(pair),
            pair=pair,
            source="price_surge",
            price_change_pct=float(m.group(2)),
            time_window_sec=int(m.group(3)),
            market_cap_usd=mcap,
            timestamp=now,
        )

    return None
