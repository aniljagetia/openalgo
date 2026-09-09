"""External (non-broker) data: global cues and institutional flows.

OpenAlgo cannot supply either of these, so both are fetched over HTTP with the
project's shared httpx client and cached, per the spec's 2-minute cue cache.

Deliberately no `yfinance` dependency. Yahoo's public chart endpoint returns
everything needed (last price and previous close) in one call, and adding a
package would mean regenerating `uv.lock` and changing the VPS requirements
files for no extra capability.

Every fetch degrades to ``None`` rather than raising. A dead cue must lower the
model's confidence, never break the page.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

CUE_CACHE_SECONDS = 120
FLOW_CACHE_SECONDS = 900  # FII/DII is a once-daily publication
HTTP_TIMEOUT = 8.0

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

NSE_FII_DII = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_HOME = "https://www.nseindia.com"


@dataclass
class Cue:
    """One global market cue.

    Attributes:
        key: Stable identifier.
        label: Display name.
        ticker: Yahoo symbol.
        bullish_for_nifty: +1 when a rise in this instrument is bullish for
            Nifty, -1 when a rise is bearish (dollar, yields, crude).
        weight: Relative weight inside the global group.
    """

    key: str
    label: str
    ticker: str
    bullish_for_nifty: int
    weight: float


# Ordered roughly by how much they set the Indian opening direction.
CUES: list[Cue] = [
    Cue("sp500_fut", "S&P 500 Futures", "ES=F", +1, 1.0),
    Cue("nasdaq_fut", "Nasdaq Futures", "NQ=F", +1, 1.0),
    Cue("dow_fut", "Dow Futures", "YM=F", +1, 0.7),
    Cue("nikkei", "Nikkei 225", "^N225", +1, 0.6),
    Cue("hangseng", "Hang Seng", "^HSI", +1, 0.6),
    Cue("kospi", "Kospi", "^KS11", +1, 0.4),
    Cue("dxy", "Dollar Index", "DX-Y.NYB", -1, 0.8),
    Cue("us10y", "US 10Y Yield", "^TNX", -1, 0.8),
    Cue("brent", "Brent Crude", "BZ=F", -1, 0.8),
    Cue("usdinr", "USD/INR", "USDINR=X", -1, 0.9),
]

_cue_cache: dict[str, Any] = {"at": 0.0, "data": {}}
_flow_cache: dict[str, Any] = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _fetch_yahoo(ticker: str) -> dict[str, float] | None:
    """Fetch last price and previous close for one Yahoo ticker.

    Returns:
        ``{"last": float, "prev_close": float, "change_pct": float}`` or None.
    """
    try:
        client = get_httpx_client()
        response = client.get(
            YAHOO_CHART.format(symbol=ticker),
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": BROWSER_UA},
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            logger.warning(f"Yahoo {ticker}: HTTP {response.status_code}")
            return None
        meta = (response.json().get("chart", {}).get("result") or [{}])[0].get("meta", {})
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if last is None or not prev:
            return None
        return {
            "last": float(last),
            "prev_close": float(prev),
            "change_pct": (float(last) - float(prev)) / float(prev) * 100.0,
        }
    except Exception as exc:  # noqa: BLE001 - a dead cue must not break scoring
        logger.warning(f"Yahoo {ticker} failed: {exc}")
        return None


def get_global_cues() -> dict[str, dict[str, Any]]:
    """Return every global cue, cached for two minutes.

    Returns:
        Mapping of cue key -> ``{"label", "change_pct", "last", ...}``. Cues
        that failed are simply absent.
    """
    now = time.time()
    with _lock:
        if now - _cue_cache["at"] < CUE_CACHE_SECONDS and _cue_cache["data"]:
            return _cue_cache["data"]

    out: dict[str, dict[str, Any]] = {}
    for cue in CUES:
        quote = _fetch_yahoo(cue.ticker)
        if quote is None:
            continue
        out[cue.key] = {
            "label": cue.label,
            "ticker": cue.ticker,
            "direction": cue.bullish_for_nifty,
            "weight": cue.weight,
            **quote,
        }

    with _lock:
        _cue_cache["at"] = now
        _cue_cache["data"] = out
    return out


def get_fii_dii() -> dict[str, float] | None:
    """Fetch the latest FII/DII net cash figures from NSE.

    NSE has no open API. This uses the endpoint the website itself calls, which
    requires a browser-like session (a homepage visit first, to pick up
    cookies). It is genuinely fragile: NSE rate-limits and frequently blocks
    datacenter IPs, so this returns None more often from a VPS than from a
    desktop. The caller treats None as *unresolved*, which is the honest
    outcome -- the signal simply does not contribute that day.

    Returns:
        ``{"fii_net": crore, "dii_net": crore}`` or None.
    """
    now = time.time()
    with _lock:
        if now - _flow_cache["at"] < FLOW_CACHE_SECONDS and _flow_cache["data"] is not None:
            return _flow_cache["data"]

    result: dict[str, float] | None = None
    try:
        client = get_httpx_client()
        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{NSE_HOME}/reports/fii-dii",
        }
        # Prime cookies; NSE rejects a cold API call.
        client.get(NSE_HOME, headers={"User-Agent": BROWSER_UA}, timeout=HTTP_TIMEOUT)
        response = client.get(NSE_FII_DII, headers=headers, timeout=HTTP_TIMEOUT)
        if response.status_code == 200:
            rows = response.json()
            parsed: dict[str, float] = {}
            for row in rows if isinstance(rows, list) else []:
                category = str(row.get("category", "")).upper()
                net = row.get("netValue")
                if net is None:
                    continue
                if "FII" in category or "FPI" in category:
                    parsed["fii_net"] = float(net)
                elif "DII" in category:
                    parsed["dii_net"] = float(net)
            result = parsed or None
        else:
            logger.warning(f"NSE FII/DII: HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"NSE FII/DII fetch failed: {exc}")

    with _lock:
        _flow_cache["at"] = now
        _flow_cache["data"] = result
    return result
