"""Multi-timeframe momentum.

The composite bias is a *lean*, not an entry trigger. What a trader actually
needs before acting is whether the last 1, 3, 5 and 15 minutes agree with that
lean, or fight it. This module answers exactly that.

Everything is derived from a single 1-minute candle series per instrument
rather than four separate history calls: a 3-minute return is just the last
close against the close three bars back. Two broker calls cover both Nifty and
BankNifty across all four timeframes.

Alignment matters more than any single timeframe. Four green rows is a trend;
green on 1m and red on 15m is noise inside a downtrend, and the panel says so.
"""

from __future__ import annotations

from typing import Any

TIMEFRAMES = (1, 3, 5, 15)


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    """Extract usable closes in chronological order."""
    out: list[float] = []
    for candle in candles:
        try:
            value = candle.get("close")
            if value is not None:
                out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _return_over(closes: list[float], bars: int) -> float | None:
    """Percent change across ``bars`` one-minute bars, or None if too short."""
    if len(closes) < bars + 1:
        return None
    past, last = closes[-(bars + 1)], closes[-1]
    if not past:
        return None
    return (last - past) / past * 100.0


def timeframe_table(
    nifty_minutes: list[dict[str, Any]],
    banknifty_minutes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the 1/3/5/15-minute momentum rows.

    Args:
        nifty_minutes: NIFTY 1-minute candles, oldest first.
        banknifty_minutes: BANKNIFTY 1-minute candles, oldest first.

    Returns:
        One row per timeframe with Nifty and BankNifty returns, a direction of
        ``"up" | "down" | "flat"``, and whether the two instruments agree.
        Rows whose data is missing report ``direction: None``.
    """
    nifty_closes = _closes(nifty_minutes)
    bank_closes = _closes(banknifty_minutes)

    rows: list[dict[str, Any]] = []
    for bars in TIMEFRAMES:
        nifty_ret = _return_over(nifty_closes, bars)
        bank_ret = _return_over(bank_closes, bars)

        if nifty_ret is None:
            direction = None
        elif nifty_ret > 0.02:
            direction = "up"
        elif nifty_ret < -0.02:
            direction = "down"
        else:
            direction = "flat"

        agree = None
        if nifty_ret is not None and bank_ret is not None:
            agree = (nifty_ret >= 0) == (bank_ret >= 0)

        rows.append(
            {
                "label": f"{bars}m",
                "minutes": bars,
                "nifty_pct": None if nifty_ret is None else round(nifty_ret, 3),
                "banknifty_pct": None if bank_ret is None else round(bank_ret, 3),
                "direction": direction,
                "banks_agree": agree,
            }
        )
    return rows


def alignment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise whether the timeframes agree with each other.

    Args:
        rows: Output of :func:`timeframe_table`.

    Returns:
        ``{"state", "score", "note"}`` where state is ``aligned_up``,
        ``aligned_down``, ``mixed`` or ``unknown``, and score is in [-1, +1].
    """
    directions = [r["direction"] for r in rows if r["direction"] is not None]
    if not directions:
        return {
            "state": "unknown",
            "score": None,
            "note": "No minute candles yet -- momentum unavailable.",
        }

    ups = directions.count("up")
    downs = directions.count("down")
    total = len(directions)
    score = (ups - downs) / total

    if ups == total:
        state, note = "aligned_up", "All timeframes rising -- momentum is with the upside."
    elif downs == total:
        state, note = "aligned_down", "All timeframes falling -- momentum is with the downside."
    else:
        state = "mixed"
        note = (
            f"Mixed: {ups} up, {downs} down, {total - ups - downs} flat across "
            "1/3/5/15m. Short and long timeframes disagree, which is chop rather "
            "than trend."
        )
    return {"state": state, "score": round(score, 3), "note": note}
