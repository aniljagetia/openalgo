"""Picking the strike to sell.

Two independent ways of choosing a short strike, reported side by side rather
than blended:

* **The OI wall** -- the strike carrying the most open interest beyond spot.
  That is where the market has already committed size, and it tends to act as
  a barrier.
* **Target delta** -- the strike whose delta is near 0.25 or 0.15. Delta is the
  market's own estimate of the chance of finishing in the money, so a 0.15
  delta short is roughly an 85% probability trade before costs.

They usually disagree by a strike or two. The recommendation takes whichever
is *further* out of the money, because for a seller the cost of being one
strike too safe is a little premium, and the cost of being one strike too close
is the trade.

Greeks come straight from the chain (``with_greeks=True``) -- verified present
and per-leg, so nothing here re-solves Black-Scholes.
"""

from __future__ import annotations

from typing import Any

# Delta targets, most aggressive first.
DELTA_TARGETS = (0.25, 0.15)

# A short option is conventionally cut when it doubles. Reported so the page
# shows an exit before it shows an entry.
STOP_MULTIPLE = 2.0


def _num(value: Any) -> float | None:
    """Coerce to float, treating None and non-numerics as missing."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def strike_step(chain: list[dict[str, Any]]) -> float | None:
    """Smallest gap between consecutive strikes, i.e. the strike interval."""
    strikes = sorted({s for row in chain if (s := _num(row.get("strike"))) is not None})
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(gaps) if gaps else None


def _legs(chain: list[dict[str, Any]], opt_type: str) -> list[tuple[float, dict[str, Any]]]:
    """Return ``(strike, leg)`` pairs for one option type, strike ascending."""
    key = "ce" if opt_type == "CE" else "pe"
    out = []
    for row in chain:
        strike = _num(row.get("strike"))
        leg = row.get(key) or {}
        if strike is not None and leg:
            out.append((strike, leg))
    return sorted(out, key=lambda p: p[0])


def _otm(pairs: list[tuple[float, dict]], spot: float, opt_type: str):
    """Filter to strikes that are out of the money for the given type."""
    if opt_type == "CE":
        return [(s, leg) for s, leg in pairs if s > spot]
    return [(s, leg) for s, leg in pairs if s < spot]


def oi_wall(chain: list[dict[str, Any]], spot: float | None, opt_type: str) -> float | None:
    """The out-of-the-money strike carrying the most open interest."""
    if spot is None:
        return None
    candidates = _otm(_legs(chain, opt_type), spot, opt_type)
    best, best_oi = None, 0.0
    for strike, leg in candidates:
        oi = _num(leg.get("oi")) or 0.0
        if oi > best_oi:
            best, best_oi = strike, oi
    return best


def delta_strike(
    chain: list[dict[str, Any]], spot: float | None, opt_type: str, target: float
) -> float | None:
    """The out-of-the-money strike whose absolute delta is closest to ``target``."""
    if spot is None:
        return None
    best, best_gap = None, None
    for strike, leg in _otm(_legs(chain, opt_type), spot, opt_type):
        delta = _num(leg.get("delta"))
        if delta is None or delta == 0:
            continue
        gap = abs(abs(delta) - target)
        if best_gap is None or gap < best_gap:
            best, best_gap = strike, gap
    return best


def describe(
    chain: list[dict[str, Any]],
    strike: float | None,
    opt_type: str,
    spot: float | None,
    reason: str,
) -> dict[str, Any] | None:
    """Build the full row for one candidate short strike.

    Returns None when the strike is not in the chain or carries no price --
    a candidate that cannot be priced is not a candidate.
    """
    if strike is None:
        return None
    leg = next((leg for s, leg in _legs(chain, opt_type) if s == strike), None)
    if not leg:
        return None
    ltp = _num(leg.get("ltp"))
    if ltp is None or ltp <= 0:
        return None
    delta = _num(leg.get("delta"))
    distance = None if spot is None else (strike - spot)
    breakeven = strike + ltp if opt_type == "CE" else strike - ltp
    return {
        "strike": strike,
        "type": opt_type,
        "symbol": leg.get("symbol"),
        "ltp": ltp,
        "bid": _num(leg.get("bid")),
        "ask": _num(leg.get("ask")),
        "oi": _num(leg.get("oi")),
        "volume": _num(leg.get("volume")),
        "iv": _num(leg.get("implied_volatility")),
        "delta": delta,
        "theta": _num(leg.get("theta")),
        "gamma": _num(leg.get("gamma")),
        "vega": _num(leg.get("vega")),
        "distance": None if distance is None else round(distance, 2),
        "distance_pct": (
            None if distance is None or not spot else round(distance / spot * 100.0, 3)
        ),
        "breakeven": round(breakeven, 2),
        "stop_loss": round(ltp * STOP_MULTIPLE, 2),
        # Delta doubles as the market's own probability of finishing ITM, so
        # 1 - |delta| is the standard rough probability of the short expiring
        # worthless. It is an estimate, and the page labels it as one.
        "prob_otm": None if delta is None else round(1 - min(1.0, abs(delta)), 3),
        "reason": reason,
    }


def candidates(
    chain: list[dict[str, Any]], spot: float | None, opt_type: str
) -> list[dict[str, Any]]:
    """Every candidate short strike for one side, nearest first.

    Duplicates are merged: when the OI wall and the 0.25-delta strike are the
    same strike, that is a stronger case, and the reasons are joined rather
    than the row repeated.
    """
    picks: list[tuple[float | None, str]] = [
        (oi_wall(chain, spot, opt_type), "highest OI beyond spot"),
    ]
    for target in DELTA_TARGETS:
        picks.append((delta_strike(chain, spot, opt_type, target), f"~{target:.2f} delta"))

    merged: dict[float, dict[str, Any]] = {}
    for strike, reason in picks:
        if strike is None:
            continue
        if strike in merged:
            merged[strike]["reason"] += f" + {reason}"
            continue
        row = describe(chain, strike, opt_type, spot, reason)
        if row:
            merged[strike] = row
    return sorted(merged.values(), key=lambda r: r["strike"], reverse=(opt_type == "PE"))


def recommend(
    chain: list[dict[str, Any]], spot: float | None, opt_type: str
) -> dict[str, Any] | None:
    """Pick the one strike to sell, plus a hedge two strikes further out.

    The hedge turns a naked short into a defined-risk spread. It is always
    offered, never assumed: the row reports the net credit and the maximum loss
    so the choice between naked and spread is made with both numbers visible.
    """
    rows = candidates(chain, spot, opt_type)
    if not rows:
        return None
    # Furthest out of the money among the candidates.
    pick = max(rows, key=lambda r: abs(r["distance"] or 0))

    step = strike_step(chain)
    hedge = None
    if step:
        hedge_strike = pick["strike"] + 2 * step if opt_type == "CE" else pick["strike"] - 2 * step
        hedge = describe(chain, hedge_strike, opt_type, spot, "protective wing")

    out = dict(pick)
    if hedge:
        credit = pick["ltp"] - hedge["ltp"]
        width = abs(hedge["strike"] - pick["strike"])
        out["hedge"] = hedge
        out["spread"] = {
            "buy_strike": hedge["strike"],
            "net_credit": round(credit, 2),
            "width": width,
            "max_loss": round(width - credit, 2),
            "risk_reward": None if credit <= 0 else round((width - credit) / credit, 2),
        }
    return out
