"""Should you be selling options at all today?

Separate from *which* side to sell. A seller can read the direction perfectly
and still lose on a day when implied volatility is under realised, the range is
expanding, or an event is pending. Each factor here scores 0..1, where 1 means
"conditions favour a seller" -- these are not directional and never say which
side to take.

The output is a single sellability score plus the reasons behind it, so the
page can say "good day, bad side" or "right side, bad day" rather than
collapsing both into one number that hides which half is wrong.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

# NSE trades 375 minutes a day, ~252 days a year. Used to annualise a realised
# volatility measured on 1-minute bars so it is comparable with quoted IV.
MINUTES_PER_YEAR = 375 * 252

# Straddle premium as a percent of spot: below thin, above rich.
STRADDLE_THIN_PCT = 0.25
STRADDLE_RICH_PCT = 1.0


def _num(value: Any) -> float | None:
    """Coerce to float, treating None and non-numerics as missing."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp01(value: float) -> float:
    """Clamp into [0, 1]."""
    return max(0.0, min(1.0, value))


def _atm_legs(chain: list[dict[str, Any]], atm_strike: float | None) -> tuple[dict, dict]:
    """Return the CE and PE legs at the ATM strike, or two empty dicts."""
    if atm_strike is None:
        return {}, {}
    for row in chain:
        if _num(row.get("strike")) == _num(atm_strike):
            return (row.get("ce") or {}), (row.get("pe") or {})
    return {}, {}


def atm_iv(chain: list[dict[str, Any]], atm_strike: float | None) -> float | None:
    """Average of the ATM call and put implied volatilities.

    Deep-ITM legs report ``implied_volatility: 0``; at the money that does not
    arise, but zeros are still filtered so a single bad leg cannot halve the
    reading.
    """
    ce, pe = _atm_legs(chain, atm_strike)
    ivs = [v for v in (_num(ce.get("implied_volatility")), _num(pe.get("implied_volatility"))) if v]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def realised_vol(minute_candles: list[dict[str, Any]]) -> float | None:
    """Annualised realised volatility from the latest session's 1-minute closes.

    Args:
        minute_candles: 1-minute candles, oldest first. Only the most recent
            session is used -- blending two sessions blends two regimes. Before
            the open that session is yesterday, which is the right baseline to
            price today's implied volatility against anyway.

    Returns:
        Annualised volatility in percent, or None with fewer than 30 usable
        bars (a reading off ten minutes of trade is noise, not volatility).
    """
    if not minute_candles:
        return None
    closes: list[float] = []
    last_day = None
    for candle in reversed(minute_candles):
        ts = _num(candle.get("timestamp"))
        close = _num(candle.get("close"))
        if ts is None or close is None:
            continue
        day = datetime.fromtimestamp(ts, IST).date()
        if last_day is None:
            last_day = day
        if day != last_day:
            break
        closes.append(close)
    closes.reverse()

    if len(closes) < 31:
        return None
    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(returns) < 30:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(MINUTES_PER_YEAR) * 100.0


def iv_vs_realised(
    chain: list[dict[str, Any]], atm_strike: float | None, minutes: list[dict[str, Any]]
) -> dict[str, Any]:
    """The seller's core edge: is implied volatility above what is realising?

    Selling options is selling implied volatility. When IV sits below realised,
    the premium collected does not cover the movement being paid for, and no
    amount of correct direction fixes that.
    """
    iv = atm_iv(chain, atm_strike)
    rv = realised_vol(minutes)
    if iv is None or rv is None or rv <= 0:
        return {
            "name": "IV vs realised",
            "score": None,
            "weight": 2.0,
            "explanation": (
                "Need both ATM implied volatility and at least 30 one-minute bars "
                "from the latest session to compare them."
            ),
            "detail": {"atm_iv": iv, "realised_vol": rv},
        }
    ratio = iv / rv
    score = _clamp01((ratio - 0.9) / 0.5)
    if ratio >= 1.3:
        verdict = "richly priced -- premium is well above what the market is delivering"
    elif ratio >= 1.05:
        verdict = "fairly priced with a margin for the seller"
    elif ratio >= 0.9:
        verdict = "thin -- barely covering realised movement"
    else:
        verdict = "under-priced: the market is moving more than options imply, which is a buyer's tape"
    return {
        "name": "IV vs realised",
        "score": round(score, 3),
        "weight": 2.0,
        "explanation": (
            f"ATM IV {iv:.1f}% against {rv:.1f}% realised in the latest session "
            f"(ratio {ratio:.2f}) -- {verdict}."
        ),
        "detail": {"atm_iv": round(iv, 2), "realised_vol": round(rv, 2), "ratio": round(ratio, 3)},
    }


def straddle_premium(
    chain: list[dict[str, Any]], atm_strike: float | None, spot: float | None
) -> dict[str, Any]:
    """How much money the ATM straddle actually pays, as a percent of spot.

    The absolute rupee premium is what gets quoted; as a share of the index it
    is comparable across days and directly answers whether the trade is worth
    the margin.
    """
    ce, pe = _atm_legs(chain, atm_strike)
    ce_ltp, pe_ltp = _num(ce.get("ltp")), _num(pe.get("ltp"))
    if ce_ltp is None or pe_ltp is None or not spot:
        return {
            "name": "Straddle premium",
            "score": None,
            "weight": 1.5,
            "explanation": "ATM call/put prices unavailable.",
            "detail": {},
        }
    total = ce_ltp + pe_ltp
    pct = total / spot * 100.0
    score = _clamp01((pct - STRADDLE_THIN_PCT) / (STRADDLE_RICH_PCT - STRADDLE_THIN_PCT))
    return {
        "name": "Straddle premium",
        "score": round(score, 3),
        "weight": 1.5,
        "explanation": (
            f"The {atm_strike:,.0f} straddle pays {total:,.1f} points ({pct:.2f}% of spot). "
            f"That is the market's own estimate of today's remaining range -- "
            f"{'worth selling' if score > 0.5 else 'thin for the risk carried'}."
        ),
        "detail": {
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "total": round(total, 2),
            "pct_of_spot": round(pct, 3),
            "implied_move": round(total, 2),
        },
    }


def vix_regime(vix: float | None) -> dict[str, Any]:
    """India VIX as a premium-richness gauge.

    Deliberately not linear. Very low VIX means there is nothing to collect;
    very high VIX means the premium is rich but the gap risk that comes with it
    is what actually kills naked sellers, so the score falls off at both ends.
    """
    if vix is None:
        return {
            "name": "VIX regime",
            "score": None,
            "weight": 1.0,
            "explanation": "India VIX unavailable.",
            "detail": {},
        }
    if vix < 10.5:
        score, note = 0.2, "very low -- there is little premium to collect for the risk"
    elif vix < 12.0:
        score, note = 0.55, "low but workable"
    elif vix <= 20.0:
        score, note = 1.0, "the healthy band for premium selling"
    elif vix <= 26.0:
        score, note = 0.6, "elevated -- premium is rich but so is the gap risk"
    else:
        score, note = 0.25, "high -- rich premium, but this is where naked sellers get hurt"
    return {
        "name": "VIX regime",
        "score": score,
        "weight": 1.0,
        "explanation": f"India VIX at {vix:.2f} is {note}.",
        "detail": {"vix": vix},
    }


def range_containment(levels: dict[str, Any]) -> dict[str, Any]:
    """Is today behaving like a range day or a trend day?

    The single most useful thing a seller can know. A day whose range is
    already 1.5x normal by noon is not going to sit still for the rest of it.
    """
    ratio = _num(levels.get("range_vs_typical"))
    if ratio is None:
        return {
            "name": "Range containment",
            "score": None,
            "weight": 1.5,
            "explanation": "Not enough completed sessions to know what a normal range is.",
            "detail": {},
        }
    score = _clamp01((1.6 - ratio) / 0.8)
    if ratio <= 0.7:
        note = "quiet -- well inside a normal day's range"
    elif ratio <= 1.1:
        note = "a normal day so far"
    elif ratio <= 1.5:
        note = "wider than usual; the tape is moving"
    else:
        note = "a trend day -- range expansion is the seller's worst environment"
    return {
        "name": "Range containment",
        "score": round(score, 3),
        "weight": 1.5,
        "explanation": (
            f"Today's range is {ratio:.2f}x the recent average ({levels.get('day_range', 0) or 0:,.0f} "
            f"points vs {levels.get('typical_range') or 0:,.0f}) -- {note}."
        ),
        "detail": {"range_vs_typical": ratio},
    }


def time_of_day(now: datetime | None = None) -> dict[str, Any]:
    """When in the session it is.

    The first half hour carries the widest ranges and the least information;
    the last half hour forces a square-off. The middle of the day is where
    theta is collected cheaply.
    """
    now = now or datetime.now(IST)
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 15:
        return {
            "name": "Time of day",
            "score": None,
            "weight": 1.0,
            "explanation": "Market has not opened; there is no intraday read yet.",
            "detail": {},
        }
    if minutes < 9 * 60 + 45:
        score, note = 0.25, "the opening half hour -- widest ranges, least information"
    elif minutes < 10 * 60 + 30:
        score, note = 0.7, "the opening move has settled"
    elif minutes < 14 * 60 + 30:
        score, note = 1.0, "the middle of the session, where theta is collected most cheaply"
    elif minutes <= 15 * 60 + 5:
        score, note = 0.75, "late session -- decay is fast but so is the squeeze risk"
    elif minutes <= 15 * 60 + 30:
        score, note = 0.3, "the closing window -- too late to open a new short"
    else:
        return {
            "name": "Time of day",
            "score": None,
            "weight": 1.0,
            "explanation": "Market is closed.",
            "detail": {},
        }
    return {
        "name": "Time of day",
        "score": score,
        "weight": 1.0,
        "explanation": f"{now:%H:%M} IST is {note}.",
        "detail": {"minutes": minutes},
    }


def days_to_expiry(expiry_ts: int | None, now: datetime | None = None) -> dict[str, Any]:
    """How close expiry is.

    Expiry day pays the most theta and carries the most gamma, and those two
    do not cancel out -- a 30-point move against an expiry-day short can
    multiply the premium several times over. It is scored below a 1-3 day
    option deliberately.
    """
    if not expiry_ts:
        return {
            "name": "Days to expiry",
            "score": None,
            "weight": 1.0,
            "explanation": "Expiry timestamp unavailable.",
            "detail": {},
        }
    now = now or datetime.now(IST)
    dte = (datetime.fromtimestamp(expiry_ts, IST).date() - now.date()).days
    if dte <= 0:
        score, note = 0.55, (
            "expiry day: maximum decay, but gamma is at its worst and a short can "
            "multiply in minutes"
        )
    elif dte <= 3:
        score, note = 1.0, "close enough to expiry for decay to be felt intraday"
    elif dte <= 7:
        score, note = 0.8, "decay is present but slower"
    else:
        score, note = 0.55, "far from expiry -- little intraday decay to collect"
    return {
        "name": "Days to expiry",
        "score": score,
        "weight": 1.0,
        "explanation": f"{dte} day(s) to expiry -- {note}.",
        "detail": {"dte": dte},
    }


def assess(
    chain: list[dict[str, Any]],
    atm_strike: float | None,
    spot: float | None,
    vix: float | None,
    minutes: list[dict[str, Any]],
    levels: dict[str, Any],
    expiry_ts: int | None,
    gate: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score the whole regime.

    Args:
        gate: Event gate from ``services.nifty_bias.events.active_gate()``. Its
            multiplier is applied to the final score rather than averaged in,
            because a pending event is a veto on naked selling, not one opinion
            among several.

    Returns:
        ``score`` 0..1, a ``verdict``, a ``confidence`` (share of intended
        weight that resolved) and every factor with its own explanation.
    """
    factors = [
        iv_vs_realised(chain, atm_strike, minutes),
        straddle_premium(chain, atm_strike, spot),
        vix_regime(vix),
        range_containment(levels),
        time_of_day(now),
        days_to_expiry(expiry_ts, now),
    ]

    total = sum(f["weight"] for f in factors) or 1.0
    resolved = [f for f in factors if f["score"] is not None]
    live = sum(f["weight"] for f in resolved)
    if not resolved or live <= 0:
        return {
            "score": None,
            "confidence": 0.0,
            "verdict": "No data",
            "note": "Nothing resolved -- the regime cannot be assessed.",
            "factors": factors,
            "gate_multiplier": 1.0,
        }

    score = sum(f["score"] * f["weight"] for f in resolved) / live
    confidence = live / total

    multiplier = float((gate or {}).get("multiplier", 1.0))
    gated = score * multiplier

    if gated >= 0.65:
        verdict = "Favourable"
        note = "Conditions support selling premium. Size normally."
    elif gated >= 0.45:
        verdict = "Marginal"
        note = "Playable, but reduce size and keep the stop tight."
    elif gated >= 0.3:
        verdict = "Poor"
        note = "The edge is thin. Prefer a spread over a naked short, or sit out."
    else:
        verdict = "Avoid"
        note = "This is not a day to be short premium."

    if multiplier < 1.0 and (gate or {}).get("note"):
        note = f"{note} {gate['note']}"

    return {
        "score": round(gated, 4),
        "raw_score": round(score, 4),
        "confidence": round(confidence, 4),
        "verdict": verdict,
        "note": note,
        "gate_multiplier": multiplier,
        "factors": factors,
    }
