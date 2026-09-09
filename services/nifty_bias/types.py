"""Shared types for the Nifty Bias Dashboard.

The scoring contract is deliberately narrow: every signal, no matter which
group it belongs to, returns a `SignalResult` carrying a score in [-1, +1],
its own weight, and a human-readable explanation. The dashboard never shows a
number it cannot explain, which is why `explanation` is required rather than
optional.

A score of `None` means *unresolved* -- the input was missing, not neutral.
An unresolved signal is excluded from the weighted mean and its weight is
redistributed, so a broken data feed cannot masquerade as a neutral market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Signal group keys. Weights live in scorer.GROUP_WEIGHTS and follow the
# spec: option chain 35%, technicals 25%, volatility 15%, global 25%.
GROUP_OPTION_CHAIN = "option_chain"
GROUP_TECHNICAL = "technical"
GROUP_VOLATILITY = "volatility"
GROUP_GLOBAL = "global"


@dataclass
class SignalResult:
    """One scored signal.

    Attributes:
        name: Display name, e.g. ``"PCR (OI)"``.
        group: One of the ``GROUP_*`` constants.
        score: Directional score in [-1, +1], or ``None`` when unresolved.
            Positive is bullish for Nifty.
        weight: Relative weight *within* its group. Normalised by the scorer,
            so these need not sum to 1.
        explanation: Why the signal scored this way, in plain English.
        detail: Optional raw numbers for the UI (e.g. the PCR value itself).
    """

    name: str
    group: str
    score: float | None
    weight: float
    explanation: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        """True when this signal produced a usable score."""
        return self.score is not None

    def clamped(self) -> float | None:
        """Score clamped into [-1, +1], or None when unresolved."""
        if self.score is None:
            return None
        return max(-1.0, min(1.0, float(self.score)))

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the JSON API."""
        return {
            "name": self.name,
            "group": self.group,
            "score": self.clamped(),
            "weight": self.weight,
            "explanation": self.explanation,
            "detail": self.detail,
            "resolved": self.resolved,
        }


@dataclass
class MarketContext:
    """Everything the signals need, fetched once per scoring cycle.

    Fetching is separated from scoring so the whole signal suite can be unit
    tested against a fixture context with no network at all.

    Attributes:
        spot_ltp: NIFTY last traded price.
        spot_prev_close: NIFTY previous close.
        vix_ltp: INDIAVIX last traded price.
        vix_prev_close: INDIAVIX previous close.
        expiry: Expiry used for the chain, DDMMMYY (e.g. ``"15SEP26"``).
        atm_strike: At-the-money strike reported by the chain.
        forward_price: Synthetic forward supplied by the chain.
        chain: Raw ``chain`` array from /optionchain.
        candles: 5-minute candles for the INDEX (no volume -- see notes).
        futures_candles: 5-minute candles for the near future (has volume+OI).
        prev_chain: The previous stored chain snapshot, for OI deltas.
        source: ``"live"`` or ``"mock"``.
        errors: Non-fatal fetch problems, surfaced in the UI data-health strip.
    """

    spot_ltp: float | None = None
    spot_prev_close: float | None = None
    vix_ltp: float | None = None
    vix_prev_close: float | None = None
    expiry: str | None = None
    atm_strike: float | None = None
    forward_price: float | None = None
    chain: list[dict[str, Any]] = field(default_factory=list)
    candles: list[dict[str, Any]] = field(default_factory=list)
    futures_candles: list[dict[str, Any]] = field(default_factory=list)
    prev_chain: list[dict[str, Any]] = field(default_factory=list)
    source: str = "live"
    errors: list[str] = field(default_factory=list)

    @property
    def spot_change_pct(self) -> float | None:
        """Percent change of spot against previous close."""
        if not self.spot_ltp or not self.spot_prev_close:
            return None
        return (self.spot_ltp - self.spot_prev_close) / self.spot_prev_close * 100.0
