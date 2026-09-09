"""Nifty 50 heavyweight constituents and their approximate index weights.

Only the names that actually move the index are listed. The tail of the Nifty
50 contributes so little that quoting all fifty would cost broker calls for no
information -- these names cover roughly two thirds of the index.

Weights are approximate NSE free-float weights and drift with rebalancing, so
they are normalised at use rather than trusted absolutely. They are only ever
used to compare constituents *against each other*, never to reprice the index.

Financials are ~30-35% of the index on their own, which is why bank moves
dominate and why `BANKNIFTY` is tracked separately as a divergence signal.
"""

from __future__ import annotations

# symbol -> approximate free-float index weight (percent)
NIFTY_HEAVYWEIGHTS: dict[str, float] = {
    "HDFCBANK": 13.2,
    "ICICIBANK": 8.9,
    "RELIANCE": 8.4,
    "INFY": 5.5,
    "BHARTIARTL": 4.4,
    "TCS": 3.9,
    "LT": 3.7,
    "ITC": 3.6,
    "AXISBANK": 3.1,
    "SBIN": 2.9,
    "KOTAKBANK": 2.6,
    "HINDUNILVR": 2.2,
    "BAJFINANCE": 2.1,
    "M&M": 2.0,
    "MARUTI": 1.7,
}

# The financial names above, used to measure whether banks are leading or
# dragging the index rather than merely participating in it.
FINANCIAL_NAMES = frozenset(
    {"HDFCBANK", "ICICIBANK", "AXISBANK", "SBIN", "KOTAKBANK", "BAJFINANCE"}
)

EQUITY_EXCHANGE = "NSE"
BANKNIFTY_SYMBOL = "BANKNIFTY"


def total_weight() -> float:
    """Sum of the tracked weights, used to normalise contributions."""
    return sum(NIFTY_HEAVYWEIGHTS.values())


def financial_weight() -> float:
    """Combined index weight of the tracked financial names."""
    return sum(w for s, w in NIFTY_HEAVYWEIGHTS.items() if s in FINANCIAL_NAMES)
