"""Data providers for the Nifty Bias Dashboard.

Two implementations sit behind one protocol, as the spec requires:

* `OpenAlgoProvider` calls this instance's own service layer directly. There is
  no HTTP hop and no second API key -- these are the same functions the
  `/api/v1` endpoints wrap.
* `MockProvider` replays the fixtures captured by `scripts/probe_chain.py`, so
  the dashboard is fully usable outside market hours and in tests.

Field shapes here were read off a live Arrow session, not assumed. See
`docs/openalgo_notes.md`. Two of those findings drive this module:

* The index has **no volume** -- every ``NSE_INDEX`` candle returns
  ``volume: 0``. VWAP therefore needs the near-month **future**, which is why
  `fetch_context` pulls a second candle series from ``NFO``.
* Legs carry no ``prev_oi``, so OI direction only exists by diffing against a
  previously stored snapshot.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from services.expiry_service import get_expiry_dates
from services.history_service import get_history
from services.option_chain_service import get_option_chain
from services.quotes_service import get_multiquotes, get_quotes
from utils.logging import get_logger

from .constituents import BANKNIFTY_SYMBOL, EQUITY_EXCHANGE, NIFTY_HEAVYWEIGHTS
from .external import get_fii_dii, get_global_cues
from .types import MarketContext

logger = get_logger(__name__)

UNDERLYING = "NIFTY"
SPOT_EXCHANGE = "NSE_INDEX"
FNO_EXCHANGE = "NFO"
VIX_SYMBOL = "INDIAVIX"
STRIKE_COUNT = 15
CANDLE_INTERVAL = "5m"
HISTORY_LOOKBACK_DAYS = 7

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "data" / "fixtures"


def to_chain_expiry(expiry: str) -> str:
    """Convert an /expiry value into the form /optionchain requires.

    ``expiry_service`` returns ``'15-SEP-26'`` while ``OptionChainSchema``
    requires DDMMMYY (``'15SEP26'``). Passing the hyphenated form straight
    through returns an empty chain with no error, so this conversion is not
    cosmetic.
    """
    return expiry.replace("-", "").upper()


def futures_symbol(expiry: str) -> str:
    """Build the near-month futures symbol from a futures expiry.

    Args:
        expiry: A futures expiry as returned by /expiry, e.g. ``'29-SEP-26'``.

    Returns:
        OpenAlgo futures symbol, e.g. ``'NIFTY29SEP26FUT'``.
    """
    return f"{UNDERLYING}{to_chain_expiry(expiry)}FUT"


class DataProvider(Protocol):
    """Everything the scoring cycle needs from the outside world."""

    def fetch_context(self, prev_chain: list[dict[str, Any]] | None = None) -> MarketContext:
        """Fetch one complete market snapshot."""
        ...


class OpenAlgoProvider:
    """Reads live data through this instance's own service layer.

    Args:
        api_key: An OpenAlgo API key belonging to the logged-in user. The
            service layer uses it to resolve the active broker session.
    """

    source = "live"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _expiries(self, instrumenttype: str) -> list[str]:
        """Return live expiries, nearest first, for options or futures."""
        ok, payload, _ = get_expiry_dates(
            symbol=UNDERLYING,
            exchange=FNO_EXCHANGE,
            instrumenttype=instrumenttype,
            api_key=self.api_key,
        )
        if not ok:
            logger.warning(f"expiry fetch failed ({instrumenttype}): {payload.get('message')}")
            return []
        return [e for e in (payload.get("data") or []) if isinstance(e, str)]

    def _quote(self, symbol: str) -> dict[str, Any]:
        """Return a single quote payload, or {} on failure."""
        ok, payload, _ = get_quotes(symbol=symbol, exchange=SPOT_EXCHANGE, api_key=self.api_key)
        if not ok:
            logger.warning(f"quote fetch failed for {symbol}: {payload.get('message')}")
            return {}
        return payload.get("data") or {}

    def _candles(
        self, symbol: str, exchange: str, interval: str = CANDLE_INTERVAL, days: int | None = None
    ) -> list[dict[str, Any]]:
        """Return candles, oldest first, or [] on failure.

        Args:
            symbol: Instrument symbol.
            exchange: Exchange code.
            interval: Candle interval; defaults to 5m.
            days: Lookback window; defaults to HISTORY_LOOKBACK_DAYS.
        """
        end = date.today()
        start = end - timedelta(days=days or HISTORY_LOOKBACK_DAYS)
        ok, payload, _ = get_history(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            api_key=self.api_key,
        )
        if not ok:
            logger.warning(f"history fetch failed for {symbol}: {payload.get('message')}")
            return []
        return payload.get("data") or []

    def _multi(self, pairs: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        """Fetch many quotes in one broker round-trip.

        Args:
            pairs: ``[{"symbol": ..., "exchange": ...}, ...]``.

        Returns:
            Mapping of symbol -> quote data. Missing symbols are simply absent.
        """
        ok, payload, _ = get_multiquotes(symbols=pairs, api_key=self.api_key)
        if not ok:
            logger.warning(f"multiquotes failed: {payload.get('message')}")
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in payload.get("results") or []:
            symbol, data = row.get("symbol"), row.get("data")
            if symbol and isinstance(data, dict):
                out[symbol] = data
        return out

    def fetch_context(self, prev_chain: list[dict[str, Any]] | None = None) -> MarketContext:
        """Fetch spot, VIX, chain, candles, constituents and external cues."""
        ctx = MarketContext(source=self.source, prev_chain=list(prev_chain or []))

        option_expiries = self._expiries("options")
        if not option_expiries:
            ctx.errors.append("No option expiries returned; is the master contract loaded?")
            return ctx
        ctx.expiry = to_chain_expiry(option_expiries[0])

        ok, chain_payload, _ = get_option_chain(
            underlying=UNDERLYING,
            exchange=SPOT_EXCHANGE,
            expiry_date=ctx.expiry,
            strike_count=STRIKE_COUNT,
            api_key=self.api_key,
            with_quotes=True,
            with_greeks=True,
        )
        if ok:
            ctx.chain = chain_payload.get("chain") or []
            ctx.atm_strike = chain_payload.get("atm_strike")
            ctx.forward_price = chain_payload.get("forward_price")
            ctx.spot_ltp = chain_payload.get("underlying_ltp")
            ctx.spot_prev_close = chain_payload.get("underlying_prev_close")
        else:
            ctx.errors.append(f"Option chain unavailable: {chain_payload.get('message')}")

        # The chain already carries spot; only fall back to a quote if it did not.
        if ctx.spot_ltp is None:
            spot = self._quote(UNDERLYING)
            ctx.spot_ltp = spot.get("ltp")
            ctx.spot_prev_close = spot.get("prev_close")

        vix = self._quote(VIX_SYMBOL)
        ctx.vix_ltp = vix.get("ltp")
        ctx.vix_prev_close = vix.get("prev_close")
        if ctx.vix_ltp is None:
            ctx.errors.append("India VIX unavailable; volatility signals degraded.")

        ctx.candles = self._candles(UNDERLYING, SPOT_EXCHANGE)

        # VWAP needs volume, which the index does not have. Use the near future.
        futures_expiries = self._expiries("futures")
        if futures_expiries:
            ctx.futures_candles = self._candles(futures_symbol(futures_expiries[0]), FNO_EXCHANGE)
        if not ctx.futures_candles:
            ctx.errors.append("Futures candles unavailable; VWAP signal disabled.")

        # Heavyweights + BANKNIFTY in a single round-trip. Financials are
        # ~30-35% of the index, so their behaviour relative to the headline
        # number is the difference between a broad move and a narrow one.
        pairs = [{"symbol": s, "exchange": EQUITY_EXCHANGE} for s in NIFTY_HEAVYWEIGHTS]
        pairs.append({"symbol": BANKNIFTY_SYMBOL, "exchange": SPOT_EXCHANGE})
        quotes = self._multi(pairs)
        bank = quotes.pop(BANKNIFTY_SYMBOL, {})
        ctx.banknifty_ltp = bank.get("ltp")
        ctx.banknifty_prev_close = bank.get("prev_close")
        ctx.constituents = quotes
        if not ctx.constituents:
            ctx.errors.append("Constituent quotes unavailable; breadth signals disabled.")

        # One 1-minute series per instrument covers 1/3/5/15m momentum; a
        # 3-minute return is just the close three bars back.
        ctx.minute_candles = self._candles(UNDERLYING, SPOT_EXCHANGE, interval="1m", days=2)
        ctx.banknifty_minutes = self._candles(
            BANKNIFTY_SYMBOL, SPOT_EXCHANGE, interval="1m", days=2
        )
        if not ctx.minute_candles:
            ctx.errors.append("1-minute candles unavailable; momentum panel disabled.")

        ctx.global_cues = get_global_cues()
        if not ctx.global_cues:
            ctx.errors.append("Global cues unavailable (Yahoo unreachable).")

        ctx.fii_dii = get_fii_dii()
        if ctx.fii_dii is None:
            ctx.errors.append(
                "FII/DII unavailable -- NSE rate-limits datacenter IPs, so this "
                "often fails from the VPS."
            )

        return ctx


class MockProvider:
    """Replays fixtures captured by ``scripts/probe_chain.py``.

    Lets the dashboard render a complete, honest-looking screen outside market
    hours. The UI always shows ``source: mock`` so a fixture is never mistaken
    for a live read.

    Args:
        fixture_dir: Directory holding the ``*_sample.json`` files.
    """

    source = "mock"

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.fixture_dir = fixture_dir or FIXTURE_DIR

    def _load(self, name: str) -> Any:
        """Load one fixture, returning None when absent."""
        path = self.fixture_dir / f"{name}.json"
        if not path.exists():
            logger.warning(f"fixture missing: {path}")
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def fetch_context(self, prev_chain: list[dict[str, Any]] | None = None) -> MarketContext:
        """Build a context from the recorded fixtures."""
        ctx = MarketContext(source=self.source, prev_chain=list(prev_chain or []))

        chain_payload = self._load("chain_sample") or {}
        ctx.chain = chain_payload.get("chain") or []
        ctx.atm_strike = chain_payload.get("atm_strike")
        ctx.forward_price = chain_payload.get("forward_price")
        ctx.spot_ltp = chain_payload.get("underlying_ltp")
        ctx.spot_prev_close = chain_payload.get("underlying_prev_close")
        ctx.expiry = chain_payload.get("expiry_date")

        quotes = self._load("quotes_sample") or {}
        vix = (quotes.get(VIX_SYMBOL) or {}).get("data") or {}
        ctx.vix_ltp = vix.get("ltp")
        ctx.vix_prev_close = vix.get("prev_close")

        history = self._load("history_sample") or {}
        ctx.candles = history.get("data") or []

        if not ctx.chain:
            ctx.errors.append("No fixtures found. Run scripts/probe_chain.py first.")
        else:
            ctx.errors.append("Mock data: fixtures from scripts/probe_chain.py, not live prices.")

        return ctx
