"""Orchestration for the Nifty Bias Dashboard.

One cycle: fetch a market snapshot, score every signal, aggregate into a
directional reading, and shape it for the UI. Kept separate from the blueprint
so it can be scheduled headlessly later without touching Flask.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

from services.nifty_bias.providers import DataProvider, MockProvider, OpenAlgoProvider
from services.nifty_bias.scorer import aggregate, narrate
from services.nifty_bias.signals import compute_all
from services.nifty_bias.types import MarketContext
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# The last chain snapshot, kept in memory so OI deltas work between polls
# without a database round-trip. Persistence lands with the collector.
_last_chain: list[dict[str, Any]] = []


def market_status(now: datetime | None = None) -> str:
    """Classify the current IST session state.

    Returns:
        One of ``"open"``, ``"pre-open"``, ``"closed"`` or ``"weekend"``.
    """
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return "weekend"
    current = now.time()
    if time(9, 0) <= current < MARKET_OPEN:
        return "pre-open"
    if MARKET_OPEN <= current <= MARKET_CLOSE:
        return "open"
    return "closed"


def _chain_rows(ctx: MarketContext) -> list[dict[str, Any]]:
    """Flatten the chain into the compact rows the UI charts."""
    rows = []
    for row in ctx.chain:
        ce, pe = row.get("ce") or {}, row.get("pe") or {}
        rows.append(
            {
                "strike": row.get("strike"),
                "ce_oi": ce.get("oi"),
                "pe_oi": pe.get("oi"),
                "ce_ltp": ce.get("ltp"),
                "pe_ltp": pe.get("ltp"),
                "ce_iv": ce.get("implied_volatility"),
                "pe_iv": pe.get("implied_volatility"),
                "ce_volume": ce.get("volume"),
                "pe_volume": pe.get("volume"),
            }
        )
    return rows


def _candle_rows(ctx: MarketContext, limit: int = 120) -> list[dict[str, Any]]:
    """Return the most recent candles, trimmed for the chart."""
    return [
        {
            "t": c.get("timestamp"),
            "o": c.get("open"),
            "h": c.get("high"),
            "l": c.get("low"),
            "c": c.get("close"),
        }
        for c in ctx.candles[-limit:]
    ]


def _levels(signals: list[Any]) -> dict[str, Any]:
    """Pull the key price levels out of the signals that computed them."""
    levels: dict[str, Any] = {}
    for sig in signals:
        if sig.name == "Max Pain":
            levels["max_pain"] = sig.detail.get("max_pain")
        elif sig.name == "OI Walls":
            levels["support"] = sig.detail.get("support")
            levels["resistance"] = sig.detail.get("resistance")
        elif sig.name == "Opening Range":
            levels["orb_high"] = sig.detail.get("orb_high")
            levels["orb_low"] = sig.detail.get("orb_low")
        elif sig.name == "VWAP (futures)":
            levels["vwap"] = sig.detail.get("vwap")
    return levels


def build_provider(api_key: str | None, use_mock: bool) -> DataProvider:
    """Choose a provider.

    Falls back to mock when no API key is available, so the page renders
    something honest rather than erroring.
    """
    if use_mock or not api_key:
        return MockProvider()
    return OpenAlgoProvider(api_key)


def get_bias(api_key: str | None = None, use_mock: bool = False) -> dict[str, Any]:
    """Run one full scoring cycle.

    Args:
        api_key: OpenAlgo API key for live data. When absent, mock is used.
        use_mock: Force the fixture provider even if a key is present.

    Returns:
        The complete dashboard payload.
    """
    global _last_chain

    provider = build_provider(api_key, use_mock)
    ctx = provider.fetch_context(prev_chain=_last_chain)

    signals = compute_all(ctx)
    result = aggregate(signals)

    # Only remember a chain we actually got, or we would wipe the baseline
    # that OI deltas depend on every time a fetch fails.
    if ctx.chain:
        _last_chain = ctx.chain

    return {
        "status": "success",
        "as_of": datetime.now(IST).isoformat(),
        "market_status": market_status(),
        "source": ctx.source,
        "errors": ctx.errors,
        "spot": {
            "ltp": ctx.spot_ltp,
            "prev_close": ctx.spot_prev_close,
            "change_pct": ctx.spot_change_pct,
        },
        "vix": {
            "ltp": ctx.vix_ltp,
            "prev_close": ctx.vix_prev_close,
        },
        "expiry": ctx.expiry,
        "atm_strike": ctx.atm_strike,
        "forward_price": ctx.forward_price,
        "probability_up": result["probability_up"],
        "composite_score": result["composite_score"],
        "confidence": result["confidence"],
        "label": result["label"],
        "narrative": narrate(result),
        "groups": result["groups"],
        "levels": _levels(signals),
        "chain": _chain_rows(ctx),
        "candles": _candle_rows(ctx),
    }
