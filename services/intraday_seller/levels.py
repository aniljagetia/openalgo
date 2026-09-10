"""Session levels: yesterday's high/low/close and today's high/low.

These are the reference points an intraday seller actually trades around -- a
short call is safe while price stays under a level that has already held, and
dangerous the moment it does not.

Everything here is derived from the 5-minute candle series the dashboard
already fetches (seven days of lookback), so no extra broker call is made.
Intraday extremes are refreshed from the 1-minute series when it is available,
because a forming 5-minute bar can be up to five minutes stale.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))


def _num(value: Any) -> float | None:
    """Coerce to float, treating None and non-numerics as missing."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _day_of(candle: dict[str, Any]) -> date | None:
    """IST calendar date of a candle, or None when the stamp is unusable."""
    ts = _num(candle.get("timestamp"))
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, IST).date()


def group_by_session(candles: list[dict[str, Any]]) -> dict[date, dict[str, float]]:
    """Roll candles up into one OHLC bar per trading day.

    Args:
        candles: Intraday candles, oldest first.

    Returns:
        ``{date: {"open", "high", "low", "close"}}``. Days whose candles are
        all malformed are omitted rather than reported with zeroes.
    """
    sessions: dict[date, dict[str, float]] = {}
    for candle in candles:
        day = _day_of(candle)
        if day is None:
            continue
        high, low = _num(candle.get("high")), _num(candle.get("low"))
        close, open_ = _num(candle.get("close")), _num(candle.get("open"))
        if high is None or low is None or close is None:
            continue
        bar = sessions.get(day)
        if bar is None:
            sessions[day] = {
                "open": open_ if open_ is not None else close,
                "high": high,
                "low": low,
                "close": close,
            }
        else:
            bar["high"] = max(bar["high"], high)
            bar["low"] = min(bar["low"], low)
            bar["close"] = close
    return sessions


def average_range(
    sessions: dict[date, dict[str, float]], lookback: int = 5, exclude_last: bool = True
) -> float | None:
    """Mean high-low range of recent completed sessions.

    The yardstick for "is today already a wide day?". While a session is live
    it is excluded, because a day that is only an hour old would drag the
    average down and make every day look wide by comparison. Before the open
    there is no live session, so nothing is excluded.
    """
    ordered = sorted(sessions)
    days = (ordered[:-1] if exclude_last else ordered)[-lookback:]
    ranges = [sessions[d]["high"] - sessions[d]["low"] for d in days]
    ranges = [r for r in ranges if r > 0]
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


def pivots(high: float, low: float, close: float) -> dict[str, float]:
    """Classic floor-trader pivots from the previous session.

    Included so the levels a short strike is chosen against do not all come
    from the option chain -- pivots are watched by enough of the market to be
    partly self-fulfilling on a range day.
    """
    pivot = (high + low + close) / 3.0
    span = high - low
    return {
        "pivot": pivot,
        "r1": 2 * pivot - low,
        "s1": 2 * pivot - high,
        "r2": pivot + span,
        "s2": pivot - span,
    }


def build(
    candles: list[dict[str, Any]],
    minute_candles: list[dict[str, Any]] | None = None,
    spot: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the full levels picture.

    Args:
        candles: 5-minute candles covering several sessions, oldest first.
        minute_candles: Recent 1-minute candles, used to keep today's high and
            low current between 5-minute bars.
        spot: Current index level, used for the range-position fields.
        now: Override for the clock, used by tests.

    Returns:
        Previous-session and current-session extremes, pivots, where price sits
        inside each range, and how today's range compares with recent days.
        Every field is None rather than 0 when it cannot be computed.

    Before the open, the newest session in the data is *yesterday*, not today.
    Calling it "today's high" would be wrong on every pre-open screen, so the
    session list is shifted by one and today's fields stay empty until the
    market has actually traded. ``session_live`` says which case applies.
    """
    now = now or datetime.now(IST)
    sessions = group_by_session(candles)
    days = sorted(sessions)

    # Has the newest session in the data actually started today?
    session_live = bool(days) and days[-1] == now.date()

    if session_live:
        today = sessions[days[-1]]
        prev = sessions[days[-2]] if len(days) >= 2 else None
    else:
        today = None
        prev = sessions[days[-1]] if days else None

    today_high = None if today is None else today["high"]
    today_low = None if today is None else today["low"]
    today_open = None if today is None else today["open"]

    # The 1-minute series is fresher: a new extreme can be printed inside the
    # 5-minute bar that is still forming.
    if session_live and minute_candles:
        latest = days[-1]
        for candle in minute_candles:
            if _day_of(candle) != latest:
                continue
            high, low = _num(candle.get("high")), _num(candle.get("low"))
            if high is not None:
                today_high = high if today_high is None else max(today_high, high)
            if low is not None:
                today_low = low if today_low is None else min(today_low, low)

    # And spot itself can print outside both series between polls. Only while
    # the session is live -- before the open the "spot" on the wire is just the
    # previous close, and folding it in would invent a zero-width range.
    if session_live and spot is not None:
        today_high = spot if today_high is None else max(today_high, spot)
        today_low = spot if today_low is None else min(today_low, spot)

    day_range = None if today_high is None or today_low is None else today_high - today_low
    typical = average_range(sessions, exclude_last=session_live)

    out: dict[str, Any] = {
        "session_live": session_live,
        "prev_high": None if prev is None else prev["high"],
        "prev_low": None if prev is None else prev["low"],
        "prev_close": None if prev is None else prev["close"],
        "prev_range": None if prev is None else prev["high"] - prev["low"],
        "today_open": today_open,
        "today_high": today_high,
        "today_low": today_low,
        "day_range": day_range,
        "typical_range": typical,
        "range_vs_typical": (
            None if day_range is None or not typical else round(day_range / typical, 3)
        ),
        "sessions_seen": len(days),
    }

    if prev is not None:
        out["pivots"] = {
            k: round(v, 2)
            for k, v in pivots(prev["high"], prev["low"], prev["close"]).items()
        }

    # Where inside each range price currently sits: 0 at the low, 1 at the high.
    if spot is not None and day_range:
        out["position_in_day"] = round((spot - today_low) / day_range, 3)
    if spot is not None and prev is not None and prev["high"] > prev["low"]:
        out["position_in_prev"] = round((spot - prev["low"]) / (prev["high"] - prev["low"]), 3)

    if spot is not None and prev is not None:
        if spot > prev["high"]:
            out["prev_range_state"] = "above"
        elif spot < prev["low"]:
            out["prev_range_state"] = "below"
        else:
            out["prev_range_state"] = "inside"

    if today_open is not None and prev is not None and prev["close"]:
        gap = (today_open - prev["close"]) / prev["close"] * 100.0
        out["gap_pct"] = round(gap, 3)
        out["gap_filled"] = (
            None
            if spot is None
            else (spot <= prev["close"] if gap > 0 else spot >= prev["close"])
        )

    return out


def level_rows(
    levels: dict[str, Any], spot: float | None, extra: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flatten every level into rows the UI can render with distances.

    Args:
        levels: Output of :func:`build`.
        spot: Current index level.
        extra: Additional named levels (VWAP, max pain, OI walls, ORB).

    Returns:
        One row per level, sorted high to low, each carrying its distance from
        spot in points and percent and which side of spot it sits on.
    """
    named: list[tuple[str, str, Any]] = [
        ("Prev day high", "prev", levels.get("prev_high")),
        ("Prev day low", "prev", levels.get("prev_low")),
        ("Prev day close", "prev", levels.get("prev_close")),
        ("Today high", "today", levels.get("today_high")),
        ("Today low", "today", levels.get("today_low")),
        ("Today open", "today", levels.get("today_open")),
    ]
    for key, label in (
        ("r2", "Pivot R2"),
        ("r1", "Pivot R1"),
        ("pivot", "Pivot"),
        ("s1", "Pivot S1"),
        ("s2", "Pivot S2"),
    ):
        named.append((label, "pivot", (levels.get("pivots") or {}).get(key)))
    for label, value in extra.items():
        named.append((label, "chain", value))

    rows = []
    for label, kind, value in named:
        price = _num(value)
        if price is None:
            continue
        distance = None if spot is None else price - spot
        rows.append(
            {
                "label": label,
                "kind": kind,
                "price": round(price, 2),
                "distance": None if distance is None else round(distance, 2),
                "distance_pct": (
                    None if distance is None or not spot else round(distance / spot * 100.0, 3)
                ),
                "side": (
                    None
                    if distance is None
                    else ("above" if distance > 0 else "below" if distance < 0 else "at")
                ),
            }
        )
    return sorted(rows, key=lambda r: r["price"], reverse=True)
