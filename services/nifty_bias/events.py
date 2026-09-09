"""Scheduled-event gate.

Policy decisions, data releases, election counts, ratings actions and index
rebalances do not have a *direction* until they land -- but they reliably
corrupt everything else. Ahead of an MPC decision or a counting day, implied
volatility inflates, option OI turns into hedging noise rather than
positioning, and the option-chain group starts reporting conviction it has not
earned.

So an event never contributes a score. It applies a **confidence multiplier**,
which damps the headline reading toward neutral and is surfaced to the user.
This is the one part of the model designed to make the dashboard *less* sure.

Events are read from ``data/nifty_bias_events.json`` so they can be maintained
without a code change. Format:

    [
      {"date": "2026-10-08", "time": "10:00", "name": "RBI MPC decision",
       "severity": "high"},
      {"date": "2026-11-14", "name": "State election counting",
       "severity": "high"}
    ]

``time`` is optional (IST); omit it for all-day events. Severity is
``high`` | ``medium`` | ``low``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
EVENTS_PATH = Path(__file__).resolve().parents[2] / "data" / "nifty_bias_events.json"

# How much of the model's confidence survives while an event is pending.
SEVERITY_MULTIPLIER = {"high": 0.45, "medium": 0.7, "low": 0.9}

# How far ahead an event starts damping confidence.
LOOKAHEAD_HOURS = {"high": 24.0, "medium": 8.0, "low": 3.0}


def load_events() -> list[dict[str, Any]]:
    """Read the event calendar, returning [] when absent or malformed."""
    if not EVENTS_PATH.exists():
        return []
    try:
        with EVENTS_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not read {EVENTS_PATH}: {exc}")
        return []


def _event_dt(event: dict[str, Any]) -> datetime | None:
    """Resolve an event's IST datetime, defaulting to market open."""
    raw_date = event.get("date")
    if not raw_date:
        return None
    try:
        day = datetime.strptime(str(raw_date), "%Y-%m-%d").date()
    except ValueError:
        logger.warning(f"Bad event date: {raw_date}")
        return None
    raw_time = str(event.get("time") or "09:15")
    try:
        hour, minute = (int(p) for p in raw_time.split(":")[:2])
    except (ValueError, TypeError):
        hour, minute = 9, 15
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def active_gate(now: datetime | None = None) -> dict[str, Any]:
    """Compute the current confidence multiplier from pending events.

    Args:
        now: Override for testing; defaults to now in IST.

    Returns:
        ``{"multiplier": float, "events": [...], "note": str}``. The multiplier
        is 1.0 when nothing is pending.
    """
    now = now or datetime.now(IST)
    pending: list[dict[str, Any]] = []
    multiplier = 1.0

    for event in load_events():
        when = _event_dt(event)
        if when is None:
            continue
        severity = str(event.get("severity", "medium")).lower()
        if severity not in SEVERITY_MULTIPLIER:
            severity = "medium"
        hours_away = (when - now).total_seconds() / 3600.0
        # Pending means ahead of us, or within the same session just past.
        if -2.0 <= hours_away <= LOOKAHEAD_HOURS[severity]:
            pending.append(
                {
                    "name": event.get("name", "Scheduled event"),
                    "severity": severity,
                    "hours_away": round(hours_away, 2),
                    "when": when.isoformat(),
                }
            )
            multiplier = min(multiplier, SEVERITY_MULTIPLIER[severity])

    if not pending:
        return {"multiplier": 1.0, "events": [], "note": ""}

    names = ", ".join(e["name"] for e in pending)
    note = (
        f"Confidence damped to {multiplier * 100:.0f}% by a pending event ({names}). "
        "Ahead of a scheduled event, IV inflates and option OI reflects hedging "
        "rather than positioning, so the chain's conviction is not trustworthy."
    )
    return {"multiplier": multiplier, "events": pending, "note": note}


def upcoming(limit: int = 6, now: datetime | None = None) -> list[dict[str, Any]]:
    """List the next scheduled events, for display rather than gating.

    Earnings dates belong here, not in the scoring model: a heavyweight's
    results are already reflected in its price and in the option chain, so
    scoring them again would count the same information twice. Showing *when*
    they land is genuinely useful; scoring them is not.

    Args:
        limit: Maximum number of events to return.
        now: Override for testing; defaults to now in IST.

    Returns:
        Upcoming events, soonest first.
    """
    now = now or datetime.now(IST)
    rows: list[dict[str, Any]] = []
    for event in load_events():
        when = _event_dt(event)
        if when is None or when < now - timedelta(hours=6):
            continue
        rows.append(
            {
                "name": event.get("name", "Scheduled event"),
                "severity": str(event.get("severity", "medium")).lower(),
                "kind": str(event.get("kind", "macro")).lower(),
                "when": when.isoformat(),
                "days_away": round((when - now).total_seconds() / 86400.0, 2),
            }
        )
    return sorted(rows, key=lambda r: r["when"])[:limit]
