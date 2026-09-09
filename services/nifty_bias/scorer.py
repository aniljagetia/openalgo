"""Weighted aggregation of signals into a single directional reading.

Two rules keep the output honest:

1. **Unresolved signals are dropped, not zeroed.** Their weight is
   redistributed across the signals that did resolve, so a missing feed lowers
   *confidence* rather than pulling the reading toward neutral.
2. **Confidence is reported separately from direction.** ``confidence`` is the
   share of the spec's intended weight that actually resolved. A strong score
   backed by 40% of the model is not the same as one backed by 100%, and the
   dashboard must not present them identically.
"""

from __future__ import annotations

from typing import Any

from .types import (
    GROUP_GLOBAL,
    GROUP_OPTION_CHAIN,
    GROUP_TECHNICAL,
    GROUP_VOLATILITY,
    SignalResult,
)

# Group weights exactly as specified.
GROUP_WEIGHTS: dict[str, float] = {
    GROUP_OPTION_CHAIN: 0.35,
    GROUP_TECHNICAL: 0.25,
    GROUP_VOLATILITY: 0.15,
    GROUP_GLOBAL: 0.25,
}

GROUP_LABELS: dict[str, str] = {
    GROUP_OPTION_CHAIN: "Option Chain",
    GROUP_TECHNICAL: "Technicals",
    GROUP_VOLATILITY: "Volatility",
    GROUP_GLOBAL: "Global & Flows",
}


def _label_for(score: float, confidence: float) -> str:
    """Turn a composite score into a plain-English bias label."""
    if confidence < 0.25:
        return "Insufficient Data"
    if score >= 0.45:
        return "Strongly Bullish"
    if score >= 0.15:
        return "Mildly Bullish"
    if score > -0.15:
        return "Neutral"
    if score > -0.45:
        return "Mildly Bearish"
    return "Strongly Bearish"


def score_group(signals: list[SignalResult]) -> tuple[float | None, float]:
    """Aggregate one group's signals.

    Args:
        signals: All signals belonging to a single group.

    Returns:
        ``(score, resolved_fraction)`` where score is the weighted mean of the
        resolved signals (None when none resolved), and resolved_fraction is
        the share of the group's intended signal weight that resolved.
    """
    total_weight = sum(s.weight for s in signals) or 0.0
    resolved = [s for s in signals if s.resolved]
    resolved_weight = sum(s.weight for s in resolved)
    if not resolved or resolved_weight <= 0:
        return None, 0.0
    weighted = sum((s.clamped() or 0.0) * s.weight for s in resolved) / resolved_weight
    fraction = resolved_weight / total_weight if total_weight > 0 else 0.0
    return weighted, fraction


def aggregate(signals: list[SignalResult]) -> dict[str, Any]:
    """Combine all signals into the dashboard's headline reading.

    Args:
        signals: Every signal from every group.

    Returns:
        A dict with the composite score, probability, label, confidence and a
        per-group breakdown including each group's contribution to the total.
    """
    groups: list[dict[str, Any]] = []
    live_weight = 0.0
    weighted_sum = 0.0

    for key, group_weight in GROUP_WEIGHTS.items():
        members = [s for s in signals if s.group == key]
        group_score, resolved_fraction = score_group(members)
        if group_score is not None:
            live_weight += group_weight
            weighted_sum += group_score * group_weight
        groups.append(
            {
                "key": key,
                "label": GROUP_LABELS[key],
                "weight": group_weight,
                "score": group_score,
                "resolved_fraction": round(resolved_fraction, 3),
                "signals": [s.to_dict() for s in members],
            }
        )

    composite = weighted_sum / live_weight if live_weight > 0 else 0.0
    confidence = live_weight / sum(GROUP_WEIGHTS.values())

    # Contribution is what each group actually added to the composite, so the
    # bars in the UI sum to the headline number rather than merely ranking.
    for group in groups:
        if group["score"] is None or live_weight <= 0:
            group["contribution"] = 0.0
        else:
            group["contribution"] = round(group["score"] * group["weight"] / live_weight, 4)

    probability_up = max(0.0, min(1.0, 0.5 + composite / 2.0))

    return {
        "composite_score": round(composite, 4),
        "probability_up": round(probability_up, 4),
        "confidence": round(confidence, 4),
        "label": _label_for(composite, confidence),
        "groups": groups,
    }


def narrate(result: dict[str, Any]) -> str:
    """Write the plain-English paragraph naming the top contributing signals."""
    resolved: list[SignalResult | dict[str, Any]] = []
    for group in result["groups"]:
        for sig in group["signals"]:
            if sig["resolved"]:
                resolved.append(sig)

    if not resolved:
        return "No signal resolved -- the dashboard has no live data to reason from."

    ranked = sorted(resolved, key=lambda s: abs(s["score"] or 0.0), reverse=True)[:3]
    label = result["label"]
    prob = result["probability_up"] * 100.0
    confidence_pct = result["confidence"] * 100.0

    parts = [
        f"{label}: a {prob:.0f}% chance Nifty closes higher, "
        f"on {confidence_pct:.0f}% of the model's intended inputs."
    ]
    for sig in ranked:
        lean = "bullish" if (sig["score"] or 0) > 0 else "bearish"
        parts.append(f"{sig['name']} ({lean}) -- {sig['explanation']}")
    return " ".join(parts)
