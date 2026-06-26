"""
Option Chain Historical OI Service

Fetches the closing OI from a 1-minute candle at `now - lookback_minutes`
ago for a list of option symbols. Used by the OptionChain page to
back the Build Up / Trend classifier with REAL broker-historical data
instead of relying on previous-poll in-memory snapshots.

Why a separate service / endpoint from `option_chain_service`:
  - History calls are rate-limited (3 req/sec via history_service);
    40-80 option symbols sequentially is 13-30s.
  - We don't want to block the main /optionchain response on that.
  - Frontend calls /optionchain (instant) + /optionchain/historical-oi
    (slow first time, instant after cache warms) in parallel and
    merges client-side.

Caching:
  - 60-second TTL in-memory cache keyed by (symbol, exchange, lookback_bucket_minute).
  - 1-minute candles only update once per minute, so any historical
    lookup within the same minute returns identical data — cache it.
  - Cache is per-process (single gunicorn worker per CLAUDE.md).

Output shape (per symbol):
  {
    "prev_oi": <int>,           # closing OI of the candle at the lookback
    "candle_ts": <ISO string>,  # when that candle ended
  }
"""

from datetime import datetime, timedelta
from typing import Any

from database.auth_db import get_auth_token_broker
from services.history_service import get_history
from utils.logging import get_logger

logger = get_logger(__name__)


# Cache: (symbol, exchange, lookback_bucket_minute) -> {"prev_oi", "candle_ts", "cached_at"}
# Cleared whenever entries exceed _CACHE_MAX_ENTRIES.
_oi_history_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ENTRIES = 5000


def _cache_key(symbol: str, exchange: str, lookback_minutes: int) -> tuple[str, str, str]:
    """Cache bucket keyed by symbol + exchange + the minute-of-day that
    the lookback resolves to. Floors the lookback target to the minute
    so all calls within the same minute share a cache entry."""
    target = datetime.now() - timedelta(minutes=lookback_minutes)
    bucket = target.strftime("%Y-%m-%dT%H:%M")
    return (symbol, exchange, bucket)


def _gc_cache() -> None:
    """Drop stale entries when the cache crosses the size threshold.
    Stale = older than _CACHE_TTL_SECONDS. Cheap O(n) sweep — fine at
    a 5000-entry cap."""
    if len(_oi_history_cache) <= _CACHE_MAX_ENTRIES:
        return
    now = datetime.now()
    threshold = now - timedelta(seconds=_CACHE_TTL_SECONDS)
    stale_keys = [
        k for k, v in _oi_history_cache.items() if v.get("cached_at_dt", now) < threshold
    ]
    for k in stale_keys:
        _oi_history_cache.pop(k, None)
    logger.info(f"OI-history cache GC: removed {len(stale_keys)} stale entries")


def _candle_oi_at(
    candles: list[dict[str, Any]],
    target_dt: datetime,
) -> tuple[float | None, str | None]:
    """Find the candle in the list whose timestamp is closest to but not
    after `target_dt`, and return its closing OI. Candles are expected
    in chronological order with `timestamp` as an epoch second (broker
    convention)."""
    if not candles:
        return None, None
    target_ts = target_dt.timestamp()
    # Iterate in reverse to find the latest candle <= target.
    best = None
    for c in reversed(candles):
        ts_raw = c.get("timestamp") or c.get("ts") or c.get("time")
        if ts_raw is None:
            continue
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            continue
        if ts <= target_ts:
            best = c
            break
    if best is None:
        # No candle at or before target — use the earliest available.
        best = candles[0]
    oi = best.get("oi")
    if oi is None:
        return None, None
    try:
        oi_val = float(oi)
    except (TypeError, ValueError):
        return None, None
    candle_ts_iso = datetime.fromtimestamp(
        float(best.get("timestamp") or best.get("ts") or best.get("time") or 0)
    ).isoformat()
    return oi_val, candle_ts_iso


def get_historical_oi_at_lookback(
    symbols: list[dict[str, str]],
    lookback_minutes: int,
    api_key: str,
) -> tuple[bool, dict[str, Any], int]:
    """Fetch the closing OI from `lookback_minutes` ago for each option
    symbol. Hits the 60s cache first; cache misses fall through to
    broker historical via `get_history`.

    Args:
        symbols: List of {"symbol": str, "exchange": str} dicts.
        lookback_minutes: How many minutes ago to read OI from.
        api_key: OpenAlgo API key — used by history_service to look up
            broker auth tokens.

    Returns:
        Tuple of (success, {"status": str, "data": {symbol: {prev_oi, candle_ts}}}, status_code).
    """
    if not symbols:
        return True, {"status": "success", "data": {}}, 200
    if lookback_minutes <= 0:
        return (
            False,
            {"status": "error", "message": "lookback_minutes must be positive"},
            400,
        )

    # Authenticate once before iterating — fail fast on bad api_key.
    try:
        auth_token, _, broker = get_auth_token_broker(api_key, include_feed_token=True)
    except Exception as e:
        logger.exception(f"Auth lookup failed: {e}")
        return False, {"status": "error", "message": "Authentication failed"}, 401
    if not auth_token or not broker:
        return False, {"status": "error", "message": "Invalid API key"}, 401

    # Historical fetch window: a 4-minute slice centered ~lookback ago.
    # 1-minute candles snap to minute boundaries — 4 min gives us a
    # buffer in case the broker returns one bar's worth offset, while
    # staying tiny so the response is small.
    now = datetime.now()
    target_dt = now - timedelta(minutes=lookback_minutes)
    fetch_start = (target_dt - timedelta(minutes=2)).strftime("%Y-%m-%d")
    fetch_end = (target_dt + timedelta(minutes=2)).strftime("%Y-%m-%d")

    out: dict[str, dict[str, Any]] = {}
    misses = 0

    for entry in symbols:
        symbol = entry.get("symbol")
        exchange = entry.get("exchange")
        if not symbol or not exchange:
            continue

        cache_key = _cache_key(symbol, exchange, lookback_minutes)
        cached = _oi_history_cache.get(cache_key)
        if cached and (now - cached["cached_at_dt"]).total_seconds() < _CACHE_TTL_SECONDS:
            out[symbol] = {
                "prev_oi": cached["prev_oi"],
                "candle_ts": cached["candle_ts"],
            }
            continue

        misses += 1
        try:
            ok, resp, _code = get_history(
                symbol=symbol,
                exchange=exchange,
                interval="1m",
                start_date=fetch_start,
                end_date=fetch_end,
                auth_token=auth_token,
                broker=broker,
            )
        except Exception as e:
            logger.exception(f"History fetch failed for {symbol}: {e}")
            out[symbol] = {"prev_oi": None, "candle_ts": None}
            continue

        if not ok:
            out[symbol] = {"prev_oi": None, "candle_ts": None}
            continue

        candles = resp.get("data") or []
        prev_oi, candle_ts = _candle_oi_at(candles, target_dt)
        out[symbol] = {"prev_oi": prev_oi, "candle_ts": candle_ts}
        _oi_history_cache[cache_key] = {
            "prev_oi": prev_oi,
            "candle_ts": candle_ts,
            "cached_at_dt": now,
        }

    _gc_cache()
    logger.info(
        f"Historical OI: {len(symbols)} symbols, lookback={lookback_minutes}min, "
        f"{misses} broker calls ({len(symbols) - misses} cache hits)"
    )
    return True, {"status": "success", "data": out}, 200
