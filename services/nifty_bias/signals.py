"""Signal computations for the Nifty Bias Dashboard.

Every function takes a `MarketContext` and returns `SignalResult`s scored in
[-1, +1], positive meaning bullish for Nifty.

An input we do not have yields ``score=None`` (*unresolved*) rather than 0.
That distinction matters: 0 asserts "the market is balanced", while None
asserts "we do not know". The scorer drops unresolved signals and
redistributes their weight, so a dead feed degrades the confidence of the
reading instead of silently dragging it toward neutral.

Group weights follow the spec exactly: option chain 35%, technicals 25%,
volatility 15%, global 25%.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .types import (
    GROUP_GLOBAL,
    GROUP_OPTION_CHAIN,
    GROUP_TECHNICAL,
    GROUP_VOLATILITY,
    MarketContext,
    SignalResult,
)

IST = timezone(timedelta(hours=5, minutes=30))

# A tanh-free, bounded mapping: how many "units" of a measure map to full
# conviction. Kept explicit so each signal's sensitivity is reviewable.
PCR_NEUTRAL = 1.0
PCR_FULL_SWING = 0.5  # PCR of 1.5 (or 0.5) is full conviction
IV_SKEW_FULL = 2.0  # 2 IV points of put-over-call skew is full conviction
MAXPAIN_FULL_PCT = 1.0  # spot 1% from max pain is full conviction
VIX_FULL_PCT = 10.0  # a 10% VIX move in a session is full conviction
RSI_FULL = 30.0  # RSI 80 or 20 is full conviction


def _bounded(value: float, full_scale: float) -> float:
    """Map a raw deviation onto [-1, +1] linearly, clipped at ``full_scale``."""
    if full_scale <= 0:
        return 0.0
    return max(-1.0, min(1.0, value / full_scale))


def _num(value: Any) -> float | None:
    """Coerce to float, treating None and non-numerics as missing."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _iv(leg: dict[str, Any]) -> float | None:
    """Read a leg's implied volatility, treating 0 as missing.

    Deep-ITM legs come back with ``implied_volatility: 0`` (verified live on
    22900 CE). Reading that as zero volatility would drag any skew or smile
    average down, so it is treated as absent.
    """
    iv = _num(leg.get("implied_volatility"))
    if iv is None or iv <= 0:
        return None
    return iv


# --------------------------------------------------------------------------
# Group A -- Option chain (35%)
# --------------------------------------------------------------------------


def pcr_oi(ctx: MarketContext) -> SignalResult:
    """Put/Call ratio by open interest.

    Read conventionally: more put OI than call OI means writers are selling
    puts and expect support, which is bullish.
    """
    ce = sum(_num(r.get("ce", {}).get("oi")) or 0.0 for r in ctx.chain)
    pe = sum(_num(r.get("pe", {}).get("oi")) or 0.0 for r in ctx.chain)
    if ce <= 0 or pe <= 0:
        return SignalResult(
            "PCR (OI)", GROUP_OPTION_CHAIN, None, 1.0, "No open interest in the chain."
        )
    pcr = pe / ce
    score = _bounded(pcr - PCR_NEUTRAL, PCR_FULL_SWING)
    lean = "bullish" if score > 0 else "bearish"
    return SignalResult(
        "PCR (OI)",
        GROUP_OPTION_CHAIN,
        score,
        1.0,
        f"PCR {pcr:.2f} ({pe:,.0f} put OI vs {ce:,.0f} call OI) leans {lean}.",
        {"pcr": round(pcr, 3), "ce_oi": ce, "pe_oi": pe},
    )


def pcr_volume(ctx: MarketContext) -> SignalResult:
    """Put/Call ratio by traded volume -- a faster, noisier PCR."""
    ce = sum(_num(r.get("ce", {}).get("volume")) or 0.0 for r in ctx.chain)
    pe = sum(_num(r.get("pe", {}).get("volume")) or 0.0 for r in ctx.chain)
    if ce <= 0 or pe <= 0:
        return SignalResult(
            "PCR (Volume)", GROUP_OPTION_CHAIN, None, 0.6, "No traded volume in the chain."
        )
    pcr = pe / ce
    score = _bounded(pcr - PCR_NEUTRAL, PCR_FULL_SWING)
    return SignalResult(
        "PCR (Volume)",
        GROUP_OPTION_CHAIN,
        score,
        0.6,
        f"Volume PCR {pcr:.2f} across {ce + pe:,.0f} contracts traded.",
        {"pcr": round(pcr, 3)},
    )


def max_pain(ctx: MarketContext) -> SignalResult:
    """Max pain strike and spot's distance from it.

    Max pain is the strike at which option writers lose least. Price is often
    argued to drift toward it into expiry, so spot below max pain is read as
    an upward pull.
    """
    strikes = [_num(r.get("strike")) for r in ctx.chain]
    strikes = [s for s in strikes if s is not None]
    if not strikes or not ctx.spot_ltp:
        return SignalResult("Max Pain", GROUP_OPTION_CHAIN, None, 1.0, "Chain or spot missing.")

    def total_pain(expiry_price: float) -> float:
        """Total writer loss if the market expired at ``expiry_price``."""
        pain = 0.0
        for row in ctx.chain:
            strike = _num(row.get("strike"))
            if strike is None:
                continue
            ce_oi = _num(row.get("ce", {}).get("oi")) or 0.0
            pe_oi = _num(row.get("pe", {}).get("oi")) or 0.0
            if expiry_price > strike:
                pain += (expiry_price - strike) * ce_oi
            if expiry_price < strike:
                pain += (strike - expiry_price) * pe_oi
        return pain

    pain_by_strike = {s: total_pain(s) for s in strikes}
    mp = min(pain_by_strike, key=lambda s: pain_by_strike[s])
    gap_pct = (mp - ctx.spot_ltp) / ctx.spot_ltp * 100.0
    score = _bounded(gap_pct, MAXPAIN_FULL_PCT)
    direction = "above" if gap_pct > 0 else "below"
    return SignalResult(
        "Max Pain",
        GROUP_OPTION_CHAIN,
        score,
        1.0,
        f"Max pain {mp:,.0f} is {abs(gap_pct):.2f}% {direction} spot {ctx.spot_ltp:,.1f}.",
        {"max_pain": mp, "gap_pct": round(gap_pct, 3)},
    )


def oi_walls(ctx: MarketContext) -> SignalResult:
    """Support/resistance from the heaviest put and call OI strikes.

    Highest call OI acts as resistance, highest put OI as support. Spot's
    position inside that band is the signal: near support is read as room to
    bounce, near resistance as room to stall.
    """
    if not ctx.chain or not ctx.spot_ltp:
        return SignalResult("OI Walls", GROUP_OPTION_CHAIN, None, 0.8, "Chain or spot missing.")

    def heaviest(side: str) -> tuple[float | None, float]:
        best_strike, best_oi = None, 0.0
        for row in ctx.chain:
            oi = _num(row.get(side, {}).get("oi")) or 0.0
            strike = _num(row.get("strike"))
            if strike is not None and oi > best_oi:
                best_strike, best_oi = strike, oi
        return best_strike, best_oi

    resistance, _ = heaviest("ce")
    support, _ = heaviest("pe")
    if resistance is None or support is None or resistance <= support:
        return SignalResult(
            "OI Walls", GROUP_OPTION_CHAIN, None, 0.8, "Could not resolve a support/resistance band."
        )

    position = (ctx.spot_ltp - support) / (resistance - support)
    # position 0 -> at support (+1 bullish), 1 -> at resistance (-1 bearish)
    score = max(-1.0, min(1.0, (0.5 - position) * 2.0))
    return SignalResult(
        "OI Walls",
        GROUP_OPTION_CHAIN,
        score,
        0.8,
        f"Spot {ctx.spot_ltp:,.1f} sits {position * 100:.0f}% up the "
        f"{support:,.0f}-{resistance:,.0f} OI band.",
        {"support": support, "resistance": resistance, "position": round(position, 3)},
    )


def iv_skew(ctx: MarketContext) -> SignalResult:
    """ATM put IV minus ATM call IV.

    Puts bid over calls means the market is paying up for downside protection,
    which is bearish. Verified live that CE and PE IV genuinely differ, so this
    is a real signal rather than a constant zero.
    """
    if not ctx.atm_strike:
        return SignalResult("IV Skew", GROUP_OPTION_CHAIN, None, 0.8, "No ATM strike.")
    row = next((r for r in ctx.chain if _num(r.get("strike")) == ctx.atm_strike), None)
    if row is None:
        return SignalResult("IV Skew", GROUP_OPTION_CHAIN, None, 0.8, "ATM strike not in chain.")
    ce_iv, pe_iv = _iv(row.get("ce", {})), _iv(row.get("pe", {}))
    if ce_iv is None or pe_iv is None:
        return SignalResult("IV Skew", GROUP_OPTION_CHAIN, None, 0.8, "ATM IV unavailable.")

    skew = pe_iv - ce_iv
    score = -_bounded(skew, IV_SKEW_FULL)
    if abs(skew) < 0.01:
        note = "Calls and puts priced level at ATM (put-call parity)."
    else:
        richer = "Puts" if skew > 0 else "Calls"
        note = f"{richer} richer by {abs(skew):.2f} IV points at {ctx.atm_strike:,.0f}."
    return SignalResult(
        "IV Skew",
        GROUP_OPTION_CHAIN,
        score,
        0.8,
        note,
        {"ce_iv": ce_iv, "pe_iv": pe_iv, "skew": round(skew, 3)},
    )


def futures_premium(ctx: MarketContext) -> SignalResult:
    """Forward price versus spot.

    The chain supplies ``forward_price`` directly, so this costs no extra
    call. A forward above spot means carry/positioning is long.
    """
    if not ctx.forward_price or not ctx.spot_ltp:
        return SignalResult(
            "Futures Premium", GROUP_OPTION_CHAIN, None, 0.6, "Forward price unavailable."
        )
    prem_pct = (ctx.forward_price - ctx.spot_ltp) / ctx.spot_ltp * 100.0
    score = _bounded(prem_pct, 0.4)
    state = "premium" if prem_pct > 0 else "discount"
    return SignalResult(
        "Futures Premium",
        GROUP_OPTION_CHAIN,
        score,
        0.6,
        f"Forward {ctx.forward_price:,.1f} is at a {abs(prem_pct):.2f}% {state} to spot.",
        {"forward": ctx.forward_price, "premium_pct": round(prem_pct, 3)},
    )


def oi_buildup(ctx: MarketContext) -> SignalResult:
    """Net CE/PE OI change since the previous stored snapshot.

    Legs carry no ``prev_oi``, so this only resolves once the collector has
    stored at least one earlier snapshot. Until then it stays unresolved
    rather than pretending the market is balanced.
    """
    if not ctx.prev_chain:
        return SignalResult(
            "OI Build-up",
            GROUP_OPTION_CHAIN,
            None,
            1.0,
            "Waiting for a second snapshot -- legs carry no prev_oi, so OI direction "
            "only exists once two readings have been stored.",
        )
    prev = {_num(r.get("strike")): r for r in ctx.prev_chain}
    ce_delta = pe_delta = 0.0
    for row in ctx.chain:
        old = prev.get(_num(row.get("strike")))
        if not old:
            continue
        ce_delta += (_num(row.get("ce", {}).get("oi")) or 0.0) - (
            _num(old.get("ce", {}).get("oi")) or 0.0
        )
        pe_delta += (_num(row.get("pe", {}).get("oi")) or 0.0) - (
            _num(old.get("pe", {}).get("oi")) or 0.0
        )
    total = abs(ce_delta) + abs(pe_delta)
    if total <= 0:
        return SignalResult(
            "OI Build-up", GROUP_OPTION_CHAIN, None, 1.0, "No OI change between snapshots."
        )
    # Put writing (PE OI up) is bullish; call writing (CE OI up) is bearish.
    score = max(-1.0, min(1.0, (pe_delta - ce_delta) / total))
    return SignalResult(
        "OI Build-up",
        GROUP_OPTION_CHAIN,
        score,
        1.0,
        f"Since last snapshot: put OI {pe_delta:+,.0f}, call OI {ce_delta:+,.0f}.",
        {"ce_delta": ce_delta, "pe_delta": pe_delta},
    )


# --------------------------------------------------------------------------
# Group B -- Technicals (25%)
# --------------------------------------------------------------------------


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    """Extract closes in order, skipping malformed candles."""
    out = []
    for c in candles:
        v = _num(c.get("close"))
        if v is not None:
            out.append(v)
    return out


def _session_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the candles belonging to the most recent session (IST)."""
    if not candles:
        return []
    last_ts = _num(candles[-1].get("timestamp"))
    if last_ts is None:
        return candles
    last_day = datetime.fromtimestamp(last_ts, IST).date()
    return [
        c
        for c in candles
        if (ts := _num(c.get("timestamp"))) is not None
        and datetime.fromtimestamp(ts, IST).date() == last_day
    ]


def ema_trend(ctx: MarketContext) -> SignalResult:
    """Price versus a 20-period EMA on 5-minute closes."""
    closes = _closes(ctx.candles)
    if len(closes) < 20:
        return SignalResult("EMA20 (5m)", GROUP_TECHNICAL, None, 1.0, "Not enough candles.")
    k = 2 / 21
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    last = closes[-1]
    gap_pct = (last - ema) / ema * 100.0
    score = _bounded(gap_pct, 0.5)
    side = "above" if gap_pct > 0 else "below"
    return SignalResult(
        "EMA20 (5m)",
        GROUP_TECHNICAL,
        score,
        1.0,
        f"Price {last:,.1f} is {abs(gap_pct):.2f}% {side} its 20-EMA ({ema:,.1f}).",
        {"ema20": round(ema, 2), "gap_pct": round(gap_pct, 3)},
    )


def rsi(ctx: MarketContext) -> SignalResult:
    """Wilder RSI(14) on 5-minute closes."""
    closes = _closes(ctx.candles)
    period = 14
    if len(closes) < period + 1:
        return SignalResult("RSI(14) 5m", GROUP_TECHNICAL, None, 1.0, "Not enough candles.")
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        value = 100.0
    else:
        value = 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    score = _bounded(value - 50.0, RSI_FULL)
    return SignalResult(
        "RSI(14) 5m",
        GROUP_TECHNICAL,
        score,
        1.0,
        f"RSI {value:.1f} on the 5-minute chart.",
        {"rsi": round(value, 2)},
    )


def vwap(ctx: MarketContext) -> SignalResult:
    """Price versus session VWAP, computed on the near-month future.

    The index reports zero volume on every candle, so VWAP is undefined there.
    This uses the futures series instead -- see ``docs/openalgo_notes.md``.
    """
    session = _session_candles(ctx.futures_candles)
    pv = vol = 0.0
    for c in session:
        v = _num(c.get("volume")) or 0.0
        high, low, close = _num(c.get("high")), _num(c.get("low")), _num(c.get("close"))
        if v <= 0 or None in (high, low, close):
            continue
        pv += (high + low + close) / 3.0 * v
        vol += v
    if vol <= 0:
        return SignalResult(
            "VWAP (futures)",
            GROUP_TECHNICAL,
            None,
            1.0,
            "No futures volume available; the index itself reports none.",
        )
    value = pv / vol
    last = _num(session[-1].get("close"))
    if last is None:
        return SignalResult("VWAP (futures)", GROUP_TECHNICAL, None, 1.0, "No futures close.")
    gap_pct = (last - value) / value * 100.0
    score = _bounded(gap_pct, 0.4)
    side = "above" if gap_pct > 0 else "below"
    return SignalResult(
        "VWAP (futures)",
        GROUP_TECHNICAL,
        score,
        1.0,
        f"Futures {last:,.1f} trade {abs(gap_pct):.2f}% {side} session VWAP ({value:,.1f}).",
        {"vwap": round(value, 2), "gap_pct": round(gap_pct, 3)},
    )


def opening_range(ctx: MarketContext) -> SignalResult:
    """Opening-range breakout status using the first 15 minutes of the session."""
    session = _session_candles(ctx.candles)
    if len(session) < 3:
        return SignalResult("Opening Range", GROUP_TECHNICAL, None, 0.8, "Session too young.")
    opening = session[:3]  # three 5-minute candles == first 15 minutes
    highs = [h for c in opening if (h := _num(c.get("high"))) is not None]
    lows = [low for c in opening if (low := _num(c.get("low"))) is not None]
    last = _num(session[-1].get("close"))
    if not highs or not lows or last is None:
        return SignalResult("Opening Range", GROUP_TECHNICAL, None, 0.8, "Malformed candles.")
    orb_high, orb_low = max(highs), min(lows)
    if last > orb_high:
        score, note = 1.0, f"Broken above the opening range high {orb_high:,.1f}."
    elif last < orb_low:
        score, note = -1.0, f"Broken below the opening range low {orb_low:,.1f}."
    else:
        span = orb_high - orb_low
        score = 0.0 if span <= 0 else (last - (orb_high + orb_low) / 2) / (span / 2) * 0.4
        note = f"Inside the opening range {orb_low:,.1f}-{orb_high:,.1f}."
    return SignalResult(
        "Opening Range",
        GROUP_TECHNICAL,
        score,
        0.8,
        note,
        {"orb_high": orb_high, "orb_low": orb_low},
    )


def prev_day_levels(ctx: MarketContext) -> SignalResult:
    """Spot versus the previous session's close."""
    if not ctx.spot_ltp or not ctx.spot_prev_close:
        return SignalResult("Prev Close", GROUP_TECHNICAL, None, 0.6, "Previous close missing.")
    change_pct = ctx.spot_change_pct or 0.0
    score = _bounded(change_pct, 0.8)
    side = "above" if change_pct > 0 else "below"
    return SignalResult(
        "Prev Close",
        GROUP_TECHNICAL,
        score,
        0.6,
        f"Spot is {abs(change_pct):.2f}% {side} the previous close "
        f"({ctx.spot_prev_close:,.1f}).",
        {"change_pct": round(change_pct, 3)},
    )


# --------------------------------------------------------------------------
# Group C -- Volatility (15%)
# --------------------------------------------------------------------------


def vix_change(ctx: MarketContext) -> SignalResult:
    """India VIX direction. Rising VIX is risk-off, hence bearish."""
    if not ctx.vix_ltp or not ctx.vix_prev_close:
        return SignalResult("India VIX", GROUP_VOLATILITY, None, 1.0, "India VIX unavailable.")
    change_pct = (ctx.vix_ltp - ctx.vix_prev_close) / ctx.vix_prev_close * 100.0
    score = -_bounded(change_pct, VIX_FULL_PCT)
    direction = "up" if change_pct > 0 else "down"
    return SignalResult(
        "India VIX",
        GROUP_VOLATILITY,
        score,
        1.0,
        f"VIX {ctx.vix_ltp:.2f}, {direction} {abs(change_pct):.2f}% "
        f"from {ctx.vix_prev_close:.2f}.",
        {"vix": ctx.vix_ltp, "change_pct": round(change_pct, 3)},
    )


def vix_level(ctx: MarketContext) -> SignalResult:
    """Absolute India VIX level as a calm/fear reading."""
    if not ctx.vix_ltp:
        return SignalResult("VIX Level", GROUP_VOLATILITY, None, 0.8, "India VIX unavailable.")
    # Below ~12 is historically calm for Nifty; above ~20 is stressed.
    score = _bounded(15.0 - ctx.vix_ltp, 5.0)
    mood = "calm" if ctx.vix_ltp < 15 else "elevated"
    return SignalResult(
        "VIX Level",
        GROUP_VOLATILITY,
        score,
        0.8,
        f"VIX at {ctx.vix_ltp:.2f} is {mood} by historical standards.",
        {"vix": ctx.vix_ltp},
    )


def vix_percentile(ctx: MarketContext) -> SignalResult:
    """30-day VIX percentile -- needs daily VIX history, not yet wired."""
    return SignalResult(
        "VIX Percentile",
        GROUP_VOLATILITY,
        None,
        0.6,
        "Needs 30 days of daily India VIX history; not yet collected.",
    )


# --------------------------------------------------------------------------
# Group D -- Global / flows (25%)
# --------------------------------------------------------------------------


def global_cues(ctx: MarketContext) -> SignalResult:
    """Global risk proxies. Requires yfinance, wired in a later step."""
    return SignalResult(
        "Global Cues",
        GROUP_GLOBAL,
        None,
        1.0,
        "S&P/Nasdaq futures, DXY, US 10Y, Brent and GIFT Nifty come from "
        "yfinance; not yet wired.",
    )


def fii_dii_flows(ctx: MarketContext) -> SignalResult:
    """Previous-session FII/DII net cash.

    There is no source for this in OpenAlgo or yfinance. It stays permanently
    unresolved until a source is chosen, and its weight is redistributed.
    """
    return SignalResult(
        "FII/DII Flows",
        GROUP_GLOBAL,
        None,
        1.0,
        "No data source available in OpenAlgo or yfinance -- needs an NSE feed "
        "or manual entry.",
    )


ALL_SIGNALS = [
    pcr_oi,
    pcr_volume,
    max_pain,
    oi_walls,
    iv_skew,
    futures_premium,
    oi_buildup,
    ema_trend,
    rsi,
    vwap,
    opening_range,
    prev_day_levels,
    vix_change,
    vix_level,
    vix_percentile,
    global_cues,
    fii_dii_flows,
]


def compute_all(ctx: MarketContext) -> list[SignalResult]:
    """Run every signal, isolating failures so one bad signal cannot break the page."""
    from utils.logging import get_logger

    logger = get_logger(__name__)
    results: list[SignalResult] = []
    for fn in ALL_SIGNALS:
        try:
            results.append(fn(ctx))
        except Exception as exc:  # noqa: BLE001 - one signal must not kill the page
            logger.exception(f"signal {fn.__name__} failed: {exc}")
            results.append(
                SignalResult(
                    fn.__name__, GROUP_OPTION_CHAIN, None, 0.0, f"Signal failed: {exc}"
                )
            )
    return results
