"""Orchestration for the Intraday Option Seller dashboard.

One cycle answers three questions in order, and keeps them separate on purpose:

1. **Where are we?** Yesterday's high/low/close, today's high/low, pivots, VWAP,
   max pain and the OI walls -- with the distance from spot to each.
2. **Which side?** Sell a call or sell a put (or a strangle when the factors
   genuinely cancel).
3. **Should we sell at all?** The volatility and range regime, which can veto
   the answer to question 2 without changing it.

The market snapshot and the signal scoring are shared with the Nifty Bias
Dashboard via ``nifty_bias_service.run_cycle``, so opening both pages does not
double the broker traffic.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from services.intraday_seller import levels as levels_mod
from services.intraday_seller import premium, side, strikes
from services.nifty_bias.events import upcoming
from services.nifty_bias_service import key_levels, market_status, run_cycle
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

TIMEFRAME_MINUTES = (1, 3, 5, 15)

# Rolling (epoch, side_score) history so the page can show whether the case for
# a side is building or fading. In-memory and single-worker, exactly like the
# bias page's history: it resets on restart and only fills while something is
# polling. Durable history arrives with the collector.
_side_history: deque[tuple[float, float]] = deque(maxlen=400)


def _side_deltas(now_ts: float) -> list[dict[str, Any]]:
    """Change in the side score over 1/3/5/15 minutes.

    A window with no observation old enough returns None, never 0 -- the page
    must not imply the case was steady when it simply was not running yet.
    """
    current = _side_history[-1][1] if _side_history else None
    out = []
    for minutes in TIMEFRAME_MINUTES:
        cutoff = now_ts - minutes * 60
        past = next((v for ts, v in _side_history if ts <= cutoff), None)
        out.append(
            {
                "label": f"{minutes}m",
                "delta": None if past is None or current is None else round(current - past, 3),
            }
        )
    return out


def _chain_levels(signals: list[Any]) -> dict[str, Any]:
    """Named levels the option chain and technicals already computed."""
    found = key_levels(signals)
    return {
        "VWAP (futures)": found.get("vwap"),
        "Max pain": found.get("max_pain"),
        "Call OI wall": found.get("resistance"),
        "Put OI wall": found.get("support"),
        "Opening range high": found.get("orb_high"),
        "Opening range low": found.get("orb_low"),
    }


def _invalidation(
    lv: dict[str, Any], chain_levels: dict[str, Any], spot: float | None, opt_type: str
) -> dict[str, Any] | None:
    """The nearest level that, if broken, kills the trade.

    A short call is wrong the moment price takes out the first meaningful level
    above it, and a short put the moment it loses the first one below. Naming
    that level before entry is the whole point -- a stop on premium alone tells
    you that you are losing, not why.
    """
    if spot is None:
        return None
    pool = {
        "Today high": lv.get("today_high"),
        "Prev day high": lv.get("prev_high"),
        "Call OI wall": chain_levels.get("Call OI wall"),
        "Pivot R1": (lv.get("pivots") or {}).get("r1"),
    }
    if opt_type == "PE":
        pool = {
            "Today low": lv.get("today_low"),
            "Prev day low": lv.get("prev_low"),
            "Put OI wall": chain_levels.get("Put OI wall"),
            "Pivot S1": (lv.get("pivots") or {}).get("s1"),
        }
    wanted = [
        (label, float(price))
        for label, price in pool.items()
        if price is not None
        and ((float(price) > spot) if opt_type == "CE" else (float(price) < spot))
    ]
    if not wanted:
        return None
    label, price = min(wanted, key=lambda p: abs(p[1] - spot))
    return {
        "label": label,
        "price": round(price, 2),
        "distance": round(price - spot, 2),
        "note": (
            f"A sustained move {'above' if opt_type == 'CE' else 'below'} {price:,.1f} "
            f"({label}) invalidates the short {'call' if opt_type == 'CE' else 'put'} -- "
            "exit there rather than waiting for the premium stop."
        ),
    }


def _plan(
    side_result: dict[str, Any],
    regime: dict[str, Any],
    ce: dict[str, Any] | None,
    pe: dict[str, Any] | None,
    lv: dict[str, Any],
    chain_levels: dict[str, Any],
    spot: float | None,
) -> dict[str, Any]:
    """Turn the side call and the regime into one concrete instruction.

    The two axes are combined here and nowhere else, so the page can always
    show *why* an instruction was downgraded: a good side on a bad day reads
    "right side, wrong day" rather than silently becoming a weak signal.
    """
    chosen = side_result.get("side")
    regime_score = regime.get("score")

    legs: list[dict[str, Any]] = []
    if chosen == "sell_call" and ce:
        legs = [ce]
    elif chosen == "sell_put" and pe:
        legs = [pe]
    elif chosen == "strangle":
        legs = [leg for leg in (ce, pe) if leg]

    if side_result.get("score") is None:
        action, headline = "wait", "No directional read"
        rationale = "Not enough resolved factors to take a side."
    elif regime_score is None:
        action, headline = "wait", "Regime unknown"
        rationale = (
            "The side is readable but the volatility regime is not, and selling "
            "premium blind to the regime is the wrong half of the trade."
        )
    elif regime_score < 0.3:
        action, headline = "stand_aside", f"Right side, wrong day ({regime['verdict']})"
        rationale = (
            f"The factors lean {_side_label(chosen)}, but {regime['note'].lower()} "
            "The direction is not the problem today."
        )
    elif regime_score < 0.45:
        action = "spread"
        headline = f"{_side_label(chosen)} -- defined risk only"
        rationale = (
            f"{regime['note']} Take the credit as a spread rather than a naked short, "
            "and size down."
        )
    elif not legs:
        action, headline = "wait", "No priceable strike"
        rationale = "The chain returned no candidate strike that could be priced."
    else:
        action = "sell"
        headline = _side_label(chosen)
        rationale = (
            f"{side_result['strength'].lower()} lean ({side_result['score']:+.2f}) with a "
            f"{regime['verdict'].lower()} selling regime ({regime_score:.2f})."
        )

    for leg in legs:
        leg["invalidation"] = _invalidation(lv, chain_levels, spot, leg["type"])

    return {
        "action": action,
        "headline": headline,
        "rationale": rationale,
        "legs": legs,
        "confidence": round(
            min(side_result.get("confidence") or 0.0, regime.get("confidence") or 0.0), 3
        ),
    }


def _side_label(side_key: str | None) -> str:
    """Human name for a side key."""
    return {
        "sell_call": "Sell calls",
        "sell_put": "Sell puts",
        "strangle": "Sell both sides (strangle)",
    }.get(side_key or "", "No side")


def get_snapshot(api_key: str | None = None, use_mock: bool = False) -> dict[str, Any]:
    """Run one full intraday option-seller cycle.

    Args:
        api_key: OpenAlgo API key for live data. When absent, mock is used.
        use_mock: Force the fixture provider even if a key is present.

    Returns:
        The complete dashboard payload.
    """
    cycle = run_cycle(api_key, use_mock)
    ctx, signals = cycle["ctx"], cycle["signals"]
    now_ts = cycle["now_ts"]

    spot = ctx.spot_ltp
    lv = levels_mod.build(ctx.candles, ctx.minute_candles, spot)
    chain_levels = _chain_levels(signals)

    side_factors = side.compute(
        lv, spot, cycle["result"], cycle["alignment"], chain_levels.get("VWAP (futures)")
    )
    side_result = side.aggregate(side_factors)

    if side_result["score"] is not None:
        _side_history.append((now_ts, side_result["score"]))

    regime = premium.assess(
        chain=ctx.chain,
        atm_strike=ctx.atm_strike,
        spot=spot,
        vix=ctx.vix_ltp,
        minutes=ctx.minute_candles,
        levels=lv,
        expiry_ts=ctx.expiry_ts,
        gate=cycle["gate"],
    )

    ce = strikes.recommend(ctx.chain, spot, "CE")
    pe = strikes.recommend(ctx.chain, spot, "PE")

    return {
        "status": "success",
        "as_of": datetime.now(IST).isoformat(),
        "market_status": market_status(),
        "source": ctx.source,
        "errors": ctx.errors,
        "spot": {
            "ltp": spot,
            "prev_close": ctx.spot_prev_close,
            "change_pct": ctx.spot_change_pct,
        },
        "vix": {"ltp": ctx.vix_ltp, "prev_close": ctx.vix_prev_close},
        "expiry": ctx.expiry,
        "atm_strike": ctx.atm_strike,
        "levels": lv,
        "level_rows": levels_mod.level_rows(lv, spot, chain_levels),
        "side": side_result,
        "side_deltas": _side_deltas(now_ts),
        "regime": regime,
        "plan": _plan(side_result, regime, ce, pe, lv, chain_levels, spot),
        "call_candidates": strikes.candidates(ctx.chain, spot, "CE"),
        "put_candidates": strikes.candidates(ctx.chain, spot, "PE"),
        "timeframes": cycle["timeframes"],
        "alignment": cycle["alignment"],
        "events": upcoming(limit=4),
        "gate": cycle["gate"],
    }
