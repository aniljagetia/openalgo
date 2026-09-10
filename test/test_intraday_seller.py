"""Tests for the Intraday Option Seller decision modules.

These cover the rules that would silently produce a wrong trade rather than an
obvious error: the pre-open session shift, the "-1 means sell calls" sign
convention, unresolved staying None instead of 0, the trend-day guard that
switches off range fading, and the spread arithmetic.

Everything here is pure -- no broker, no database, no network.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from services.intraday_seller import levels as L
from services.intraday_seller import premium, side, strikes

IST = timezone(timedelta(hours=5, minutes=30))

DAY_1 = datetime(2026, 9, 8, tzinfo=IST)
DAY_2 = datetime(2026, 9, 9, tzinfo=IST)
DAY_3 = datetime(2026, 9, 10, tzinfo=IST)


def make_session(day: datetime, base: float, swing: float, bars: int = 75, minutes: int = 5):
    """Build one session of candles that oscillates around ``base``."""
    out = []
    for i in range(bars):
        stamp = day.replace(hour=9, minute=15) + timedelta(minutes=minutes * i)
        close = base + swing * math.sin(i / 7.0)
        out.append(
            {
                "timestamp": int(stamp.timestamp()),
                "open": close,
                "high": close + 8,
                "low": close - 8,
                "close": close,
                "volume": 0,
            }
        )
    return out


def make_chain(spot: float, lo: int = 23200, hi: int = 24100, step: int = 50):
    """A synthetic chain with monotonic deltas and OI walls either side."""
    def leg(strike, moneyness, oi_centre, iv):
        return {
            "symbol": f"NIFTY15SEP26{strike}",
            "ltp": round(max(0.5, 120 * math.exp(-abs(moneyness))), 2),
            "oi": int(100000 * math.exp(-abs(moneyness - oi_centre))),
            "volume": 5000,
            "implied_volatility": iv,
            "delta": 0.0,
            "gamma": 0.0004,
            "theta": -8.0,
            "vega": 5.0,
            "bid": 1.0,
            "ask": 1.2,
        }

    chain = []
    for strike in range(lo, hi + 1, step):
        moneyness = (strike - spot) / 100.0
        ce_delta = max(0.01, min(0.99, 0.5 - moneyness * 0.22))
        ce = leg(strike, moneyness, 1.5, 12.5 + abs(moneyness))
        pe = leg(strike, moneyness, -1.5, 13.5 + abs(moneyness))
        ce["delta"] = round(ce_delta, 4)
        pe["delta"] = round(-(1 - ce_delta), 4)
        chain.append({"strike": float(strike), "ce": ce, "pe": pe})
    return chain


# --------------------------------------------------------------------------
# levels
# --------------------------------------------------------------------------


def test_before_the_open_there_is_no_today():
    """The newest session in the data is yesterday until the market trades.

    Regression: pre-open the dashboard labelled the previous session's high and
    low as "today", which is the one mistake that makes every level on the page
    point at the wrong day.
    """
    candles = make_session(DAY_1, 23500, 60) + make_session(DAY_2, 23600, 40)
    result = L.build(candles, [], spot=23590.0, now=DAY_3.replace(hour=8, minute=30))

    assert result["session_live"] is False
    assert result["today_high"] is None
    assert result["today_low"] is None
    assert result["day_range"] is None
    # Yesterday's session became "prev", not "today".
    assert result["prev_high"] == pytest.approx(
        max(c["high"] for c in make_session(DAY_2, 23600, 40))
    )


def test_live_session_folds_in_spot():
    """While trading, spot can print outside both candle series."""
    candles = (
        make_session(DAY_1, 23500, 60)
        + make_session(DAY_2, 23600, 40)
        + make_session(DAY_3, 23650, 30, bars=40)
    )
    result = L.build(candles, [], spot=23999.0, now=DAY_3.replace(hour=12, minute=30))

    assert result["session_live"] is True
    assert result["today_high"] == 23999.0
    assert result["position_in_day"] == pytest.approx(1.0)


def test_no_candles_yields_no_levels_not_zeros():
    result = L.build([], [], spot=23700.0, now=DAY_3.replace(hour=12, minute=0))
    assert result["session_live"] is False
    assert result["prev_high"] is None
    assert result["today_high"] is None


def test_pivots_are_the_classic_formula():
    got = L.pivots(high=100.0, low=80.0, close=90.0)
    assert got["pivot"] == pytest.approx(90.0)
    assert got["r1"] == pytest.approx(100.0)
    assert got["s1"] == pytest.approx(80.0)
    assert got["r2"] == pytest.approx(110.0)
    assert got["s2"] == pytest.approx(70.0)


def test_level_rows_sort_high_to_low_and_carry_distance():
    levels = {"prev_high": 100.0, "prev_low": 80.0, "prev_close": 90.0}
    rows = L.level_rows(levels, spot=90.0, extra={"Max pain": 95.0})
    assert [r["price"] for r in rows] == [100.0, 95.0, 90.0, 80.0]
    assert rows[0]["side"] == "above"
    assert rows[-1]["distance"] == pytest.approx(-10.0)
    assert rows[2]["side"] == "at"


# --------------------------------------------------------------------------
# side -- the sign convention is the thing most likely to be inverted
# --------------------------------------------------------------------------


def test_positive_score_means_sell_puts():
    above = side.vwap_side(vwap=23600.0, spot=23700.0)
    below = side.vwap_side(vwap=23700.0, spot=23600.0)
    assert above.score > 0 and "sell puts" in above.explanation
    assert below.score < 0 and "sell calls" in below.explanation


def test_holding_above_yesterdays_high_favours_selling_puts():
    levels = {"prev_range_state": "above", "prev_high": 23600.0, "prev_low": 23500.0}
    assert side.prev_day_range(levels, spot=23650.0).score > 0

    levels = {"prev_range_state": "below", "prev_high": 23600.0, "prev_low": 23500.0}
    assert side.prev_day_range(levels, spot=23450.0).score < 0


def test_missing_input_is_unresolved_not_neutral():
    """None and 0 mean different things and must not be conflated."""
    assert side.vwap_side(None, 23700.0).score is None
    assert side.prev_day_range({}, None).score is None
    assert side.composite_bias(None).score is None


def test_trend_day_switches_off_the_range_fade():
    """At the high of a contained day, fade it; on a trend day, do not."""
    contained = {
        "position_in_day": 0.95,
        "today_high": 23700.0,
        "today_low": 23600.0,
        "range_vs_typical": 0.7,
    }
    trending = dict(contained, range_vs_typical=2.0)

    assert side.day_range_position(contained, 23695.0).score < -0.3
    assert side.day_range_position(trending, 23695.0).score == pytest.approx(0.0)
    assert "trend day" in side.day_range_position(trending, 23695.0).explanation


def test_composite_bias_is_scaled_by_its_own_confidence():
    full = side.composite_bias({"composite_score": -0.6, "confidence": 0.9, "label": "Bearish"})
    thin = side.composite_bias({"composite_score": -0.6, "confidence": 0.2, "label": "Bearish"})
    assert abs(thin.score) < abs(full.score)


def test_aggregate_calls_a_strangle_when_factors_cancel():
    factors = [
        side.SignalResult("A", side.GROUP_SIDE, 0.5, 1.0, ""),
        side.SignalResult("B", side.GROUP_SIDE, -0.5, 1.0, ""),
    ]
    result = side.aggregate(factors)
    assert result["side"] == "strangle"
    assert result["score"] == pytest.approx(0.0)
    assert result["confidence"] == pytest.approx(1.0)


def test_aggregate_redistributes_the_weight_of_unresolved_factors():
    factors = [
        side.SignalResult("A", side.GROUP_SIDE, 0.8, 1.0, ""),
        side.SignalResult("B", side.GROUP_SIDE, None, 3.0, ""),
    ]
    result = side.aggregate(factors)
    # The resolved factor still speaks at full strength; only confidence drops.
    assert result["score"] == pytest.approx(0.8)
    assert result["confidence"] == pytest.approx(0.25)


def test_aggregate_with_nothing_resolved_reports_no_side():
    factors = [side.SignalResult("A", side.GROUP_SIDE, None, 1.0, "")]
    result = side.aggregate(factors)
    assert result["score"] is None
    assert result["side"] == "none"


# --------------------------------------------------------------------------
# premium / regime
# --------------------------------------------------------------------------


def test_realised_vol_needs_enough_bars():
    short = [
        {"timestamp": int((DAY_3.replace(hour=9, minute=15) + timedelta(minutes=i)).timestamp()),
         "close": 23600.0 + i}
        for i in range(10)
    ]
    assert premium.realised_vol(short) is None


def test_realised_vol_uses_only_the_latest_session():
    """Two sessions must not be blended -- the gap between them is not a move."""
    bars = []
    for day, base in ((DAY_2, 23000.0), (DAY_3, 24000.0)):
        for i in range(60):
            stamp = day.replace(hour=9, minute=15) + timedelta(minutes=i)
            bars.append({"timestamp": int(stamp.timestamp()), "close": base + (i % 3)})
    value = premium.realised_vol(bars)
    assert value is not None
    # A 1000-point overnight gap would blow this up if both days were used.
    assert value < 100.0


def test_iv_above_realised_scores_well_for_a_seller():
    chain = make_chain(23650.0)
    bars = []
    for i in range(80):
        stamp = DAY_3.replace(hour=9, minute=15) + timedelta(minutes=i)
        bars.append({"timestamp": int(stamp.timestamp()), "close": 23650.0 + (i % 2)})
    result = premium.iv_vs_realised(chain, 23650.0, bars)
    assert result["score"] is not None and result["score"] > 0.5


def test_iv_vs_realised_is_unresolved_without_both_sides():
    assert premium.iv_vs_realised([], 23650.0, [])["score"] is None


def test_vix_score_falls_off_at_both_extremes():
    assert premium.vix_regime(9.0)["score"] < premium.vix_regime(15.0)["score"]
    assert premium.vix_regime(35.0)["score"] < premium.vix_regime(15.0)["score"]
    assert premium.vix_regime(None)["score"] is None


def test_expiry_day_scores_below_a_short_dated_option():
    """Maximum theta does not make expiry day the best day to be short."""
    expiry_today = int(DAY_3.replace(hour=15, minute=30).timestamp())
    expiry_in_two = int((DAY_3 + timedelta(days=2)).replace(hour=15, minute=30).timestamp())
    now = DAY_3.replace(hour=12, minute=0)
    assert (
        premium.days_to_expiry(expiry_today, now)["score"]
        < premium.days_to_expiry(expiry_in_two, now)["score"]
    )


def test_opening_half_hour_scores_worse_than_midday():
    early = premium.time_of_day(DAY_3.replace(hour=9, minute=20))["score"]
    midday = premium.time_of_day(DAY_3.replace(hour=12, minute=0))["score"]
    assert early < midday


def test_event_gate_damps_the_regime_rather_than_joining_the_average():
    chain = make_chain(23650.0)
    levels = {"range_vs_typical": 0.8, "day_range": 90, "typical_range": 110}
    now = DAY_3.replace(hour=12, minute=0)
    expiry = int((DAY_3 + timedelta(days=2)).replace(hour=15, minute=30).timestamp())

    kwargs = {
        "chain": chain,
        "atm_strike": 23650.0,
        "spot": 23650.0,
        "vix": 14.0,
        "minutes": [],
        "levels": levels,
        "expiry_ts": expiry,
        "now": now,
    }
    clear = premium.assess(gate={"multiplier": 1.0, "note": ""}, **kwargs)
    gated = premium.assess(gate={"multiplier": 0.45, "note": "RBI policy tomorrow."}, **kwargs)

    # The gate scales the final score; the ungated reading is preserved beside it.
    assert gated["raw_score"] == pytest.approx(clear["raw_score"])
    assert gated["score"] == pytest.approx(gated["raw_score"] * 0.45, abs=1e-4)
    assert clear["score"] == pytest.approx(clear["raw_score"], abs=1e-4)
    assert "RBI policy tomorrow." in gated["note"]


def test_assess_with_nothing_resolved_says_so():
    result = premium.assess(
        chain=[], atm_strike=None, spot=None, vix=None, minutes=[], levels={},
        expiry_ts=None, now=DAY_3.replace(hour=20, minute=0),
    )
    assert result["score"] is None
    assert result["verdict"] == "No data"


# --------------------------------------------------------------------------
# strikes
# --------------------------------------------------------------------------


def test_strike_step_is_the_chain_interval():
    assert strikes.strike_step(make_chain(23650.0)) == 50.0
    assert strikes.strike_step([]) is None


def test_oi_wall_is_out_of_the_money_only():
    chain = make_chain(23650.0)
    assert strikes.oi_wall(chain, 23650.0, "CE") > 23650.0
    assert strikes.oi_wall(chain, 23650.0, "PE") < 23650.0


def test_delta_strike_targets_the_requested_delta():
    chain = make_chain(23650.0)
    strike = strikes.delta_strike(chain, 23650.0, "CE", 0.15)
    leg = next(r["ce"] for r in chain if r["strike"] == strike)
    assert abs(abs(leg["delta"]) - 0.15) < 0.08


def test_recommendation_takes_the_further_strike():
    """Too safe costs a little premium; too close costs the trade."""
    chain = make_chain(23650.0)
    spot = 23650.0
    for opt_type in ("CE", "PE"):
        pick = strikes.recommend(chain, spot, opt_type)
        distances = [abs(c["distance"]) for c in strikes.candidates(chain, spot, opt_type)]
        assert abs(pick["distance"]) == max(distances)


def test_spread_arithmetic_holds():
    pick = strikes.recommend(make_chain(23650.0), 23650.0, "CE")
    spread = pick["spread"]
    assert spread["net_credit"] == pytest.approx(pick["ltp"] - pick["hedge"]["ltp"])
    assert spread["max_loss"] == pytest.approx(spread["width"] - spread["net_credit"])
    assert spread["buy_strike"] > pick["strike"]


def test_short_leg_states_its_exit():
    pick = strikes.recommend(make_chain(23650.0), 23650.0, "PE")
    assert pick["stop_loss"] == pytest.approx(pick["ltp"] * 2)
    assert pick["breakeven"] == pytest.approx(pick["strike"] - pick["ltp"])
    assert 0.0 <= pick["prob_otm"] <= 1.0


def test_unpriceable_strike_is_not_a_candidate():
    chain = make_chain(23650.0)
    for row in chain:
        row["ce"]["ltp"] = 0.0
    assert strikes.candidates(chain, 23650.0, "CE") == []
    assert strikes.recommend(chain, 23650.0, "CE") is None
