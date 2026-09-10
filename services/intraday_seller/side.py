"""Which side to sell: call or put.

Convention throughout this module: a score of **+1 means SELL PUT** and **-1
means SELL CALL**. Selling a put is the bullish-to-neutral expression (it wins
while price holds above the strike); selling a call is the bearish-to-neutral
one. Zero is a genuine "no lean", which for a seller usually means the neutral
expression -- a strangle -- rather than no trade.

As everywhere in this dashboard, a factor with no usable input scores ``None``
and is dropped from the mean with its weight redistributed. It never scores 0,
because "I do not know" and "the market is balanced" lead to different trades.
"""

from __future__ import annotations

from typing import Any

from services.nifty_bias.types import SignalResult

GROUP_SIDE = "side"

# How far price has to sit from VWAP, in percent, for full conviction.
VWAP_FULL_PCT = 0.4

# Above this multiple of the recent average range, today is a trend day and
# fading its extremes stops being an edge.
TREND_DAY_RATIO = 1.4


def _num(value: Any) -> float | None:
    """Coerce to float, treating None and non-numerics as missing."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    """Clamp into a range."""
    return max(low, min(high, value))


def _side_word(score: float) -> str:
    """Name the side a score points at."""
    if score > 0:
        return "sell puts"
    if score < 0:
        return "sell calls"
    return "neither side"


def composite_bias(bias: dict[str, Any] | None) -> SignalResult:
    """Carry the Nifty Bias composite through as the directional anchor.

    The bias dashboard already blends the option chain, technicals, volatility
    and global cues. Re-deriving that here would be a second, worse copy, so
    the seller consumes its output rather than competing with it.
    """
    score = None if bias is None else _num(bias.get("composite_score"))
    if score is None:
        return SignalResult(
            "Composite bias", GROUP_SIDE, None, 2.0, "Bias engine produced no reading."
        )
    confidence = (bias or {}).get("confidence") or 0.0
    # A composite backed by a third of the model should not drive the trade as
    # hard as one backed by all of it.
    adjusted = _clamp(score * min(1.0, float(confidence) / 0.6))
    return SignalResult(
        "Composite bias",
        GROUP_SIDE,
        adjusted,
        2.0,
        f"{bias.get('label')} ({score:+.2f} at {float(confidence) * 100:.0f}% "
        f"confidence) -- favours {_side_word(adjusted)}.",
        {"composite_score": score, "confidence": confidence},
    )


def timeframe_alignment(align: dict[str, Any] | None) -> SignalResult:
    """Whether 1/3/5/15-minute momentum agrees with itself.

    Aligned timeframes are the seller's warning, not comfort: four green rows
    means a trend is running, and the short call sitting above is the one that
    gets tested. The score therefore follows momentum -- sell the side momentum
    is running *away* from.
    """
    score = None if align is None else _num(align.get("score"))
    if score is None:
        return SignalResult(
            "1/3/5/15m alignment",
            GROUP_SIDE,
            None,
            1.5,
            "No minute candles yet -- momentum unknown.",
        )
    state = (align or {}).get("state")
    return SignalResult(
        "1/3/5/15m alignment",
        GROUP_SIDE,
        _clamp(score),
        1.5,
        f"{(align or {}).get('note')} Favours {_side_word(score)}.",
        {"state": state, "score": score},
    )


def prev_day_range(levels: dict[str, Any], spot: float | None) -> SignalResult:
    """Where price sits relative to yesterday's high and low.

    Yesterday's extremes are the levels the whole market can see. Holding above
    the previous high is the cleanest evidence a put sale below it is safe;
    losing the previous low says the same about a call sale above.
    """
    state = levels.get("prev_range_state")
    high, low = _num(levels.get("prev_high")), _num(levels.get("prev_low"))
    if state is None or high is None or low is None or spot is None:
        return SignalResult(
            "Prev day range", GROUP_SIDE, None, 1.2, "Previous session high/low unavailable."
        )
    if state == "above":
        return SignalResult(
            "Prev day range",
            GROUP_SIDE,
            0.8,
            1.2,
            f"Spot {spot:,.1f} is above yesterday's high {high:,.1f} -- that level is "
            "now support, so put sales below it have a floor to lean on.",
            {"state": state, "prev_high": high, "prev_low": low},
        )
    if state == "below":
        return SignalResult(
            "Prev day range",
            GROUP_SIDE,
            -0.8,
            1.2,
            f"Spot {spot:,.1f} is below yesterday's low {low:,.1f} -- that level is "
            "now resistance, so call sales above it have a ceiling to lean on.",
            {"state": state, "prev_high": high, "prev_low": low},
        )
    position = _num(levels.get("position_in_prev"))
    if position is None:
        return SignalResult(
            "Prev day range", GROUP_SIDE, None, 1.2, "Previous range has no width."
        )
    # Inside yesterday's range: fade whichever edge price is leaning against.
    score = _clamp(-(position - 0.5) * 2 * 0.6)
    where = "upper" if position > 0.6 else "lower" if position < 0.4 else "middle"
    return SignalResult(
        "Prev day range",
        GROUP_SIDE,
        score,
        1.2,
        f"Inside yesterday's {low:,.1f}-{high:,.1f} range, in the {where} third. "
        f"Unbroken ranges favour fading the edge, so this leans {_side_word(score)}.",
        {"state": state, "position": position, "prev_high": high, "prev_low": low},
    )


def day_range_position(levels: dict[str, Any], spot: float | None) -> SignalResult:
    """Fade today's extreme -- but only while today stays a range day.

    The fade is scaled by containment: once today's range is already wider than
    a normal session, price sitting at the high is evidence of trend, not of an
    exhausted move, and the fade edge is gone. That scaling is the difference
    between a seller's dashboard and a contrarian one.
    """
    position = _num(levels.get("position_in_day"))
    high, low = _num(levels.get("today_high")), _num(levels.get("today_low"))
    if position is None or high is None or low is None:
        return SignalResult(
            "Position in day range",
            GROUP_SIDE,
            None,
            1.0,
            "Today's high/low not established yet.",
        )
    ratio = _num(levels.get("range_vs_typical"))
    containment = 1.0 if ratio is None else _clamp(TREND_DAY_RATIO - ratio, 0.0, 1.0)
    score = _clamp(-(position - 0.5) * 2 * containment)
    if containment <= 0.05:
        note = (
            f"Today's range is already {ratio:.2f}x a normal session. That is a trend "
            "day, and fading its extremes is not a seller's edge -- no lean taken."
        )
    else:
        where = "high" if position > 0.6 else "low" if position < 0.4 else "middle"
        note = (
            f"Spot sits at {position * 100:.0f}% of today's {low:,.1f}-{high:,.1f} range "
            f"(near the {where}), in a session running "
            f"{'a normal' if ratio is None else f'{ratio:.2f}x the usual'} width. "
            f"Leans {_side_word(score)}."
        )
    return SignalResult(
        "Position in day range",
        GROUP_SIDE,
        score,
        1.0,
        note,
        {"position": position, "range_vs_typical": ratio, "containment": round(containment, 3)},
    )


def vwap_side(vwap: float | None, spot: float | None) -> SignalResult:
    """Spot versus the futures VWAP.

    VWAP is where the day's volume actually changed hands, so it is the level
    intraday desks defend. Trading above it all session is the simplest
    evidence that puts, not calls, are the safer sale.
    """
    if vwap is None or spot is None or not spot:
        return SignalResult(
            "VWAP (futures)",
            GROUP_SIDE,
            None,
            1.0,
            "Futures VWAP unavailable -- the index carries no volume, so this needs "
            "the near-month future.",
        )
    distance_pct = (spot - vwap) / vwap * 100.0
    score = _clamp(distance_pct / VWAP_FULL_PCT)
    side = "above" if distance_pct > 0 else "below"
    return SignalResult(
        "VWAP (futures)",
        GROUP_SIDE,
        score,
        1.0,
        f"Spot is {abs(distance_pct):.2f}% {side} VWAP ({vwap:,.1f}) -- favours "
        f"{_side_word(score)}.",
        {"vwap": round(vwap, 2), "distance_pct": round(distance_pct, 3)},
    )


def gap_behaviour(levels: dict[str, Any]) -> SignalResult:
    """Whether an opening gap is holding or filling.

    A gap that holds through the first hour is a directional session; a gap
    that fills is a fade, and the side worth selling flips with it.
    """
    gap = _num(levels.get("gap_pct"))
    if gap is None or abs(gap) < 0.15:
        return SignalResult(
            "Gap behaviour",
            GROUP_SIDE,
            None,
            0.6,
            "No meaningful opening gap to read.",
        )
    filled = levels.get("gap_filled")
    if filled is None:
        return SignalResult(
            "Gap behaviour", GROUP_SIDE, None, 0.6, "Previous close unavailable."
        )
    direction = 1.0 if gap > 0 else -1.0
    if filled:
        score = _clamp(-direction * 0.5)
        note = (
            f"The {abs(gap):.2f}% gap-{'up' if gap > 0 else 'down'} has been filled -- "
            f"the opening move failed, which leans {_side_word(score)}."
        )
    else:
        score = _clamp(direction * 0.7)
        note = (
            f"The {abs(gap):.2f}% gap-{'up' if gap > 0 else 'down'} is holding -- "
            f"unfilled gaps tend to run, which leans {_side_word(score)}."
        )
    return SignalResult(
        "Gap behaviour", GROUP_SIDE, score, 0.6, note, {"gap_pct": gap, "filled": filled}
    )


def open_drive(levels: dict[str, Any], spot: float | None) -> SignalResult:
    """Spot versus today's opening print."""
    open_ = _num(levels.get("today_open"))
    if open_ is None or spot is None or not open_:
        return SignalResult(
            "Vs today's open", GROUP_SIDE, None, 0.6, "Today's open unavailable."
        )
    change = (spot - open_) / open_ * 100.0
    score = _clamp(change / 0.5)
    return SignalResult(
        "Vs today's open",
        GROUP_SIDE,
        score,
        0.6,
        f"Spot is {abs(change):.2f}% {'above' if change > 0 else 'below'} today's open "
        f"({open_:,.1f}) -- leans {_side_word(score)}.",
        {"today_open": open_, "change_pct": round(change, 3)},
    )


def compute(
    levels: dict[str, Any],
    spot: float | None,
    bias: dict[str, Any] | None,
    align: dict[str, Any] | None,
    vwap: float | None,
) -> list[SignalResult]:
    """Score every directional factor."""
    return [
        composite_bias(bias),
        timeframe_alignment(align),
        prev_day_range(levels, spot),
        day_range_position(levels, spot),
        vwap_side(vwap, spot),
        gap_behaviour(levels),
        open_drive(levels, spot),
    ]


def aggregate(factors: list[SignalResult]) -> dict[str, Any]:
    """Blend the directional factors into one side call.

    Returns:
        ``score`` in [-1, +1] (+ = sell put), ``confidence`` as the share of
        intended weight that resolved, the recommended ``side`` and a
        ``strength`` label. ``side`` is ``"strangle"`` when the factors cancel:
        for a seller that is a real answer, not an abstention.
    """
    total = sum(f.weight for f in factors) or 1.0
    resolved = [f for f in factors if f.resolved]
    live = sum(f.weight for f in resolved)
    if not resolved or live <= 0:
        return {
            "score": None,
            "confidence": 0.0,
            "side": "none",
            "strength": "No data",
            "factors": [f.to_dict() for f in factors],
        }

    score = sum((f.clamped() or 0.0) * f.weight for f in resolved) / live
    confidence = live / total

    if abs(score) < 0.15:
        side, strength = "strangle", "Balanced"
    elif score > 0:
        side = "sell_put"
        strength = "Strong" if score >= 0.45 else "Moderate"
    else:
        side = "sell_call"
        strength = "Strong" if score <= -0.45 else "Moderate"

    return {
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "side": side,
        "strength": strength,
        "factors": [f.to_dict() for f in factors],
    }
