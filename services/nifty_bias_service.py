"""Orchestration for the Nifty Bias Dashboard.

One cycle: fetch a market snapshot, score every signal, aggregate into a
directional reading, and shape it for the UI. Kept separate from the blueprint
so it can be scheduled headlessly later without touching Flask.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, time, timedelta, timezone
from typing import Any

from services.nifty_bias.constituents import NIFTY_HEAVYWEIGHTS
from services.nifty_bias.events import active_gate, upcoming
from services.nifty_bias.momentum import alignment, timeframe_table
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

# Rolling (epoch_seconds, probability_up) history so the page can show whether
# the reading is strengthening or fading. In-memory and single-worker only:
# it resets on restart. Durable history arrives with the collector.
_bias_history: deque[tuple[float, float]] = deque(maxlen=400)


# Rolling per-signal score history: (epoch, {signal_name: score}). This is what
# lets every parameter report its own 1/3/5/15-minute change rather than only
# the headline. Same caveat as _bias_history: in-memory, single worker, and it
# only accumulates while something is polling.
_signal_history: deque[tuple[float, dict[str, float]]] = deque(maxlen=400)

TIMEFRAME_MINUTES = (1, 3, 5, 15)

# Rolling per-constituent LTP history. Derived from the multiquotes call we
# already make each cycle, so the heavyweight timeframe columns cost no extra
# broker requests -- fetching 1-minute candles for fifteen names would be
# fifteen more history calls per refresh.
_constituent_history: deque[tuple[float, dict[str, float]]] = deque(maxlen=400)


def _constituent_deltas(now_ts: float) -> dict[str, dict[str, float | None]]:
    """Percent price change per constituent over each timeframe.

    Returns:
        ``{symbol: {"1m": pct, "3m": ..., "5m": ..., "15m": ...}}``, with None
        for windows that have no earlier observation.
    """
    if not _constituent_history:
        return {}
    current = _constituent_history[-1][1]
    out: dict[str, dict[str, float | None]] = {}
    for symbol, price in current.items():
        row: dict[str, float | None] = {}
        for minutes in TIMEFRAME_MINUTES:
            cutoff = now_ts - minutes * 60
            snapshot = next((snap for ts, snap in _constituent_history if ts <= cutoff), None)
            past = None if snapshot is None else snapshot.get(symbol)
            row[f"{minutes}m"] = (
                None if not past else round((price - past) / past * 100.0, 3)
            )
        out[symbol] = row
    return out


def _signal_deltas(now_ts: float) -> dict[str, dict[str, float | None]]:
    """Per-signal score change over each timeframe.

    Returns:
        ``{signal_name: {"1m": delta, "3m": ..., "5m": ..., "15m": ...}}``.
        A window with no observation old enough yields None, never 0 -- the
        page must not imply a parameter was steady when it simply had no
        earlier reading to compare against.
    """
    if not _signal_history:
        return {}
    current = _signal_history[-1][1]
    out: dict[str, dict[str, float | None]] = {}
    for name, score in current.items():
        row: dict[str, float | None] = {}
        for minutes in TIMEFRAME_MINUTES:
            cutoff = now_ts - minutes * 60
            past_snapshot = next((snap for ts, snap in _signal_history if ts <= cutoff), None)
            past = None if past_snapshot is None else past_snapshot.get(name)
            row[f"{minutes}m"] = None if past is None else round(score - past, 3)
        out[name] = row
    return out


def _bias_deltas(now_ts: float) -> list[dict[str, Any]]:
    """Change in P(up), in percentage points, over 1/3/5/15 minutes.

    Returns None for a window with no observation old enough, rather than 0 --
    the page must not imply the reading was flat when it simply was not
    running yet.
    """
    out = []
    for minutes in (1, 3, 5, 15):
        cutoff = now_ts - minutes * 60
        past = next((p for ts, p in _bias_history if ts <= cutoff), None)
        current = _bias_history[-1][1] if _bias_history else None
        out.append(
            {
                "label": f"{minutes}m",
                "delta_pp": (
                    None
                    if past is None or current is None
                    else round((current - past) * 100.0, 2)
                ),
            }
        )
    return out


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


def _constituent_rows(
    ctx: MarketContext, deltas: dict[str, dict[str, float | None]] | None = None
) -> list[dict[str, Any]]:
    """Per-heavyweight rows for the UI, sorted by index impact.

    ``contribution`` is the name's weighted push on the index (its move times
    its index weight), so the list ranks by what actually moved Nifty rather
    than by headline percentage.
    """
    rows = []
    for symbol, weight in NIFTY_HEAVYWEIGHTS.items():
        quote = ctx.constituents.get(symbol) or {}
        ltp, prev = quote.get("ltp"), quote.get("prev_close")
        change_pct = None
        if ltp and prev:
            change_pct = (float(ltp) - float(prev)) / float(prev) * 100.0
        rows.append(
            {
                "symbol": symbol,
                "weight": weight,
                "ltp": ltp,
                "change_pct": change_pct,
                "contribution": None if change_pct is None else change_pct * weight / 100.0,
                "deltas": (deltas or {}).get(symbol, {}),
            }
        )
    return sorted(rows, key=lambda r: abs(r["contribution"] or 0.0), reverse=True)


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
    gate = active_gate()
    result = aggregate(signals, gate=gate)
    if gate["note"]:
        ctx.errors.append(gate["note"])

    now_ts = datetime.now(IST).timestamp()
    _bias_history.append((now_ts, result["probability_up"]))
    _signal_history.append(
        (now_ts, {s.name: s.clamped() for s in signals if s.resolved})
    )
    _constituent_history.append(
        (
            now_ts,
            {
                sym: float(q["ltp"])
                for sym, q in ctx.constituents.items()
                if q.get("ltp")
            },
        )
    )
    deltas = _signal_deltas(now_ts)
    for group in result["groups"]:
        for sig in group["signals"]:
            sig["deltas"] = deltas.get(sig["name"], {})
    tf_rows = timeframe_table(ctx.minute_candles, ctx.banknifty_minutes)

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
        "banknifty": {
            "ltp": ctx.banknifty_ltp,
            "prev_close": ctx.banknifty_prev_close,
            "change_pct": (
                None
                if not ctx.banknifty_ltp or not ctx.banknifty_prev_close
                else (ctx.banknifty_ltp - ctx.banknifty_prev_close)
                / ctx.banknifty_prev_close
                * 100.0
            ),
        },
        "constituents": _constituent_rows(ctx, _constituent_deltas(now_ts)),
        "timeframes": tf_rows,
        "alignment": alignment(tf_rows),
        "bias_deltas": _bias_deltas(now_ts),
        "events": upcoming(),
        "expiry": ctx.expiry,
        "atm_strike": ctx.atm_strike,
        "forward_price": ctx.forward_price,
        "probability_up": result["probability_up"],
        "composite_score": result["composite_score"],
        "confidence": result["confidence"],
        "label": result["label"],
        "narrative": (narrate(result) + (" " + gate["note"] if gate["note"] else "")),
        "gate": result["gate"],
        "weights": result["weights"],
        "groups": result["groups"],
        "levels": _levels(signals),
        "chain": _chain_rows(ctx),
        "candles": _candle_rows(ctx),
    }
