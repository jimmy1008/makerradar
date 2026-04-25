"""SQLite database operations with auto-migration."""

import sqlite3
import logging
from pathlib import Path

DB_PATH = Path(__file__).parent / "makerradar.db"
logger = logging.getLogger(__name__)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Schema definition ──
# Each table maps to its expected columns: {name: "TYPE DEFAULT ..."}
SCHEMA = {
    "watchlist": {
        "id":               "INTEGER PRIMARY KEY AUTOINCREMENT",
        "symbol":           "TEXT NOT NULL",
        "pair":             "TEXT NOT NULL",
        "source":           "TEXT DEFAULT ''",
        "status":           "TEXT DEFAULT 'watching'",
        "trend":            "TEXT DEFAULT ''",
        "last_scan":        "TEXT",
        "swing_low_4h":     "REAL",
        "swing_high_4h":    "REAL",
        "lowest_since":     "REAL",
        "highest_since":    "REAL",
        "vol_spike_count":  "INTEGER DEFAULT 0",
        "entry_triggered":  "INTEGER DEFAULT 0",
        "entry_price":      "REAL",
        "entry_at":         "TEXT",
        "notes":            "TEXT DEFAULT ''",
        "added_at":         "TEXT DEFAULT (datetime('now'))",
        "updated_at":       "TEXT DEFAULT (datetime('now'))",
    },
    "factor_snapshots": {
        "id":               "INTEGER PRIMARY KEY AUTOINCREMENT",
        "symbol":           "TEXT NOT NULL",
        "pair":             "TEXT NOT NULL",
        "scan_type":        "TEXT DEFAULT ''",
        "trend":            "TEXT DEFAULT ''",
        "trend_strength":   "INTEGER DEFAULT 0",
        "rsi":              "REAL",
        "kdj_k":            "REAL",
        "kdj_d":            "REAL",
        "kdj_j":            "REAL",
        "volume_ratio":     "REAL",
        "atr":              "REAL",
        "atr_pct":          "REAL",
        "decay_score":      "INTEGER DEFAULT 0",
        "reversal_score":   "INTEGER DEFAULT 0",
        "pullback_score":   "INTEGER DEFAULT 0",
        "candle_body_ratio": "REAL",
        "candle_vs_avg":    "REAL",
        "drop_from_high_pct": "REAL",
        "days_falling":     "INTEGER",
        "pullback_depth_pct": "REAL",
        "fib_level":        "REAL",
        "above_structure":  "INTEGER",
        "swing_low":        "REAL",
        "swing_high":       "REAL",
        "listing_age_days": "INTEGER",
        "market_cap_usd":   "REAL",
        "oi_change_pct":    "REAL",
        "entry_score":      "INTEGER DEFAULT 0",
        "entry_type":       "TEXT DEFAULT ''",
        "entry_price":      "REAL",
        "sl_price":         "REAL",
        "sl_pct":           "REAL",
        "tp1_price":        "REAL",
        "tp1_pct":          "REAL",
        "mfe_1h":           "REAL",
        "mfe_4h":           "REAL",
        "mfe_24h":          "REAL",
        "mfe_72h":          "REAL",
        "mae_24h":          "REAL",
        "result":           "TEXT",
        "created_at":       "TEXT DEFAULT (datetime('now'))",
    },
    "alerts": {
        "id":               "INTEGER PRIMARY KEY AUTOINCREMENT",
        "symbol":           "TEXT NOT NULL",
        "pair":             "TEXT NOT NULL",
        "source":           "TEXT NOT NULL",
        "oi_change_pct":    "REAL",
        "price_change_pct": "REAL",
        "time_window_sec":  "INTEGER",
        "oi_usd":           "REAL",
        "oi_mcap_ratio":    "REAL",
        "price_24h_pct":    "REAL",
        "market_cap_usd":   "REAL",
        "raw_text":         "TEXT",
        "ru7_score":        "INTEGER",
        "ru7_tags":         "TEXT DEFAULT ''",
        "received_at":      "TEXT DEFAULT (datetime('now'))",
    },
    "signals": {
        "id":               "INTEGER PRIMARY KEY AUTOINCREMENT",
        "symbol":           "TEXT NOT NULL DEFAULT ''",
        "pair":             "TEXT NOT NULL DEFAULT ''",
        "level":            "TEXT NOT NULL DEFAULT ''",
        "reasons":          "TEXT",
        "warnings":         "TEXT",
        "rsi":              "REAL",
        "kdj_k":            "REAL",
        "kdj_d":            "REAL",
        "kdj_j":            "REAL",
        "volume_ratio":     "REAL",
        "oi_change_pct":    "REAL",
        "price_change_pct": "REAL",
        "oi_usd":           "REAL",
        "oi_mcap_ratio":    "REAL",
        "market_cap_usd":   "REAL",
        "patterns":         "TEXT DEFAULT ''",
        "signal_type":      "TEXT DEFAULT 'opportunity'",
        "confidence":       "TEXT DEFAULT ''",
        "score":            "INTEGER DEFAULT 0",
        "listing_age":      "INTEGER",
        "vol_percentile":   "REAL",
        "chase_threshold":  "REAL",
        "hit_count":        "INTEGER DEFAULT 0",
        "social_score":     "INTEGER DEFAULT 0",
        "social_tags":      "TEXT DEFAULT ''",
        "trending_rank":    "INTEGER",
        "vol_rank_delta":   "INTEGER",
        "ru7_score":        "INTEGER",
        "ru7_tags":         "TEXT DEFAULT ''",
        "funding_rate":     "REAL",
        "ls_ratio":         "REAL",
        "top_ls_ratio":     "REAL",
        "hype_oi_usd":      "REAL",
        "hype_funding":     "REAL",
        "entry_price":      "REAL",
        "sl":               "REAL",
        "sl_pct":           "REAL",
        "tp1":              "REAL",
        "tp1_pct":          "REAL",
        "tp2":              "REAL",
        "tp2_pct":          "REAL",
        "tp3":              "REAL",
        "tp3_pct":          "REAL",
        "tp4_trail":        "REAL",
        "tp4_trail_pct":    "REAL",
        "rr":               "REAL",
        "price_1h":         "REAL",
        "price_4h":         "REAL",
        "price_24h":        "REAL",
        "pnl_1h":           "REAL",
        "pnl_4h":           "REAL",
        "pnl_24h":          "REAL",
        "high_since":       "REAL",
        "low_since":        "REAL",
        "tp_hit_at":        "TEXT",
        "sl_hit_at":        "TEXT",
        "result":           "TEXT",
        "notified":         "INTEGER DEFAULT 0",
        "created_at":       "TEXT DEFAULT (datetime('now'))",
    },
}


def _get_existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _migrate_table(conn: sqlite3.Connection, table: str, columns: dict):
    """Create table if missing, or ALTER TABLE ADD COLUMN for new columns."""
    # Check if table exists
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()

    if not exists:
        col_defs = ", ".join(f"{name} {typedef}" for name, typedef in columns.items())
        conn.execute(f"CREATE TABLE {table} ({col_defs})")
        logger.info(f"Created table: {table}")
        return

    # Table exists — check for missing columns
    existing = _get_existing_columns(conn, table)
    for col_name, col_type in columns.items():
        if col_name not in existing:
            # Strip NOT NULL for ALTER TABLE (SQLite limitation)
            safe_type = col_type.replace("NOT NULL", "").strip()
            # Ensure there's a DEFAULT for new columns
            if "DEFAULT" not in safe_type and "PRIMARY KEY" not in safe_type:
                safe_type += " DEFAULT NULL"
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {safe_type}")
                logger.info(f"Added column: {table}.{col_name}")
            except Exception as e:
                logger.warning(f"Failed to add {table}.{col_name}: {e}")


def init_db():
    conn = get_db()
    for table, columns in SCHEMA.items():
        _migrate_table(conn, table, columns)
    conn.commit()
    conn.close()


def q_all(sql: str, params=()) -> list[dict]:
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def q_one(sql: str, params=()) -> dict | None:
    conn = get_db()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return dict(row) if row else None


def q_val(sql: str, params=(), default=None):
    conn = get_db()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row[0] if row else default


def execute(sql: str, params=()):
    conn = get_db()
    conn.execute(sql, params)
    conn.commit()
    conn.close()


init_db()
