"""
Probe the live OpenAlgo instance to learn the REAL shapes of the data the
Nifty Bias Dashboard depends on, before any signal code is written.

Step 2 of the build order in `nifty_dashboard_prompt_openalgo.md`.

It answers the questions the spec told us not to guess at:

  * Does an option-chain leg already carry ``oi``? ``volume``? a previous-close
    to diff OI against? -> decides whether OI-change must be derived by diffing
    consecutive snapshots.
  * Does ``with_greeks=True`` really return ``implied_volatility`` + the greeks?
    -> decides whether a local Black-Scholes/Newton-Raphson solver is needed.
  * Is INDIAVIX quotable on this broker?
  * Does 5-minute history come back with ``oi`` on F&O exchanges?

Outputs
-------
data/fixtures/*.json   Raw responses, reused as the MockProvider corpus (step 3).
docs/openalgo_notes.md Observed field shapes, written as we go (spec requirement).

Usage
-----
    export OPENALGO_API_KEY=...            # generated at /apikey
    export OPENALGO_HOST=http://127.0.0.1:5000   # or https://algo.dhanhub.com
    uv run python scripts/probe_chain.py

Nothing here writes to the app's databases and no orders are placed --
it is read-only against the data endpoints.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "data" / "fixtures"
NOTES_PATH = REPO_ROOT / "docs" / "openalgo_notes.md"

HOST = os.environ.get("OPENALGO_HOST", "http://127.0.0.1:5000").rstrip("/")
API_KEY = os.environ.get("OPENALGO_API_KEY", "")

UNDERLYING = "NIFTY"
SPOT_EXCHANGE = "NSE_INDEX"
FNO_EXCHANGE = "NFO"
STRIKE_COUNT = 15
TIMEOUT = 30.0

# Fields the dashboard's signals need. Presence is reported per leg so a
# missing one becomes a decision, not a runtime surprise.
REQUIRED_LEG_FIELDS = [
    "symbol",
    "ltp",
    "oi",
    "volume",
    "prev_close",
    "prev_oi",
    "bid",
    "ask",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
]


class ProbeError(RuntimeError):
    """Raised when the instance answers but the payload is unusable."""


def post(client: httpx.Client, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to an /api/v1 endpoint and return the decoded body.

    Args:
        client: Shared httpx client (closed by the caller's context manager).
        endpoint: Endpoint name, e.g. ``"optionchain"``.
        payload: Request body; the API key is injected here.

    Returns:
        The decoded JSON response.

    Raises:
        ProbeError: If the response is not valid JSON.
    """
    url = f"{HOST}/api/v1/{endpoint}"
    body = {"apikey": API_KEY, **payload}
    printable = {k: ("***" if k == "apikey" else v) for k, v in body.items()}
    print(f"\n>>> POST {url}\n    {json.dumps(printable)}")

    response = client.post(url, json=body)
    try:
        data = response.json()
    except ValueError as exc:
        raise ProbeError(f"{endpoint} returned non-JSON (HTTP {response.status_code})") from exc

    status = data.get("status", "?")
    print(f"<<< HTTP {response.status_code}  status={status}")
    if status != "success":
        print(f"    message: {data.get('message')}")
    return data


def describe(value: Any, depth: int = 0, max_depth: int = 3) -> str:
    """Render a compact type/shape description of a decoded JSON value.

    Lists are collapsed to their first element so a 30-strike chain prints as
    one representative leg rather than 30 near-identical ones.
    """
    indent = "  " * depth
    if isinstance(value, dict):
        if depth >= max_depth:
            return f"{{...{len(value)} keys...}}"
        lines = []
        for key, val in value.items():
            lines.append(f"{indent}  {key}: {describe(val, depth + 1, max_depth)}")
        return "{\n" + "\n".join(lines) + f"\n{indent}}}"
    if isinstance(value, list):
        if not value:
            return "[] (empty)"
        return f"[{len(value)} x {describe(value[0], depth + 1, max_depth)}]"
    if value is None:
        return "null"
    return f"{type(value).__name__} = {value!r}"


def save_fixture(name: str, data: Any) -> Path:
    """Write a response to data/fixtures/ for the MockProvider to replay."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"    saved -> {path.relative_to(REPO_ROOT)}")
    return path


def audit_leg_fields(chain: list[dict[str, Any]]) -> dict[str, bool]:
    """Report which of REQUIRED_LEG_FIELDS actually appear on a CE leg.

    Args:
        chain: The ``chain`` array from an optionchain response.

    Returns:
        Mapping of field name -> present on the sampled leg.
    """
    if not chain:
        return dict.fromkeys(REQUIRED_LEG_FIELDS, False)
    sample = chain[len(chain) // 2].get("ce") or {}
    return {field: field in sample for field in REQUIRED_LEG_FIELDS}


def to_chain_expiry(expiry: str) -> str:
    """Convert an /expiry date into the format /optionchain demands.

    The two endpoints disagree: ``/api/v1/expiry`` returns ``'31-JUL-25'``
    (verified in ``services/expiry_service.py``, which parses its own output
    with ``%d-%b-%y``), while ``OptionChainSchema`` documents ``expiry_date``
    as DDMMMYY, e.g. ``'28NOV25'``. Passing the hyphenated form straight
    through silently yields an empty chain.

    Args:
        expiry: Expiry as returned by /api/v1/expiry, e.g. ``'31-JUL-25'``.

    Returns:
        The same date as ``'31JUL25'``.
    """
    return expiry.replace("-", "").upper()


def probe_expiries(client: httpx.Client) -> list[str]:
    """Fetch option expiries for the underlying.

    Returns:
        Expiry strings in the service's own ``'31-JUL-25'`` format, already
        filtered to live contracts and sorted nearest-first by the service.
    """
    data = post(
        client,
        "expiry",
        {"symbol": UNDERLYING, "exchange": FNO_EXCHANGE, "instrumenttype": "options"},
    )
    save_fixture("expiry_sample", data)
    expiries = [e for e in (data.get("data") or []) if isinstance(e, str)]
    print(f"    expiries: {expiries[:6]}")
    return expiries


def probe_chain(client: httpx.Client, expiry: str) -> dict[str, Any]:
    """Fetch the option chain WITH greeks -- the dashboard's primary input."""
    data = post(
        client,
        "optionchain",
        {
            "underlying": UNDERLYING,
            "exchange": SPOT_EXCHANGE,
            "expiry_date": expiry,
            "strike_count": STRIKE_COUNT,
            "with_greeks": True,
        },
    )
    save_fixture("chain_sample", data)
    return data


def probe_quotes(client: httpx.Client) -> dict[str, Any]:
    """Fetch NIFTY spot and INDIAVIX quotes."""
    out: dict[str, Any] = {}
    for symbol in (UNDERLYING, "INDIAVIX"):
        out[symbol] = post(client, "quotes", {"symbol": symbol, "exchange": SPOT_EXCHANGE})
    save_fixture("quotes_sample", out)
    return out


def probe_history(client: httpx.Client) -> dict[str, Any]:
    """Fetch 5-minute candles for the technical signals."""
    end = date.today()
    start = end - timedelta(days=7)
    data = post(
        client,
        "history",
        {
            "symbol": UNDERLYING,
            "exchange": SPOT_EXCHANGE,
            "interval": "5m",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    )
    save_fixture("history_sample", data)
    return data


def write_notes(sections: list[tuple[str, str]], audit: dict[str, bool]) -> None:
    """Write docs/openalgo_notes.md from the observed shapes."""
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# OpenAlgo data notes (observed, not assumed)",
        "",
        f"Generated by `scripts/probe_chain.py` against `{HOST}`.",
        "Every shape below was read off a live response -- if a signal needs a field",
        "that is `MISSING` here, it must be derived, not assumed.",
        "",
        "## Option-chain leg field audit",
        "",
        "| Field | Present |",
        "| --- | --- |",
    ]
    for field, present in audit.items():
        lines.append(f"| `{field}` | {'yes' if present else '**MISSING**'} |")

    missing = [f for f, present in audit.items() if not present]
    lines += ["", "### Consequences", ""]
    if "implied_volatility" in missing:
        lines.append("- IV absent -> local Black-Scholes solver required (spec fallback).")
    else:
        lines.append("- IV present via `with_greeks=True` -> **no local solver needed**.")
    if "prev_oi" in missing:
        lines.append(
            "- No previous OI on the leg -> OI-change must be derived by diffing "
            "consecutive stored snapshots. First run after startup has no delta."
        )
    else:
        lines.append("- `prev_oi` present -> OI-change is directly computable per snapshot.")

    for title, body in sections:
        lines += ["", f"## {title}", "", "```", body, "```"]
    lines.append("")

    NOTES_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nnotes -> {NOTES_PATH.relative_to(REPO_ROOT)}")


def main() -> int:
    """Run every probe and write fixtures + notes."""
    if not API_KEY:
        print("ERROR: set OPENALGO_API_KEY (generate one at /apikey).", file=sys.stderr)
        return 2

    print(f"Probing OpenAlgo at {HOST}")
    sections: list[tuple[str, str]] = []

    with httpx.Client(timeout=TIMEOUT) as client:
        try:
            expiries = probe_expiries(client)
        except (httpx.HTTPError, ProbeError) as exc:
            print(f"\nERROR: cannot reach {HOST}: {exc}", file=sys.stderr)
            print("Is the instance running and the API key valid?", file=sys.stderr)
            return 1

        if not expiries:
            print("\nERROR: no expiries returned -- is the master contract loaded?", file=sys.stderr)
            return 1

        expiry = expiries[0]
        chain_expiry = to_chain_expiry(expiry)
        print(f"\nUsing nearest (weekly) expiry: {expiry} -> {chain_expiry} for /optionchain")

        chain = probe_chain(client, chain_expiry)
        sections.append(("optionchain (with_greeks=True)", describe(chain)))

        quotes = probe_quotes(client)
        sections.append(("quotes (NIFTY, INDIAVIX)", describe(quotes)))

        history = probe_history(client)
        sections.append(("history (5m)", describe(history)))

    audit = audit_leg_fields(chain.get("chain") or [])
    print("\n--- option-chain leg field audit ---")
    for field, present in audit.items():
        print(f"  {field:22} {'ok' if present else 'MISSING'}")

    write_notes(sections, audit)
    print("\nDone. Fixtures in data/fixtures/ are the MockProvider corpus for step 3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
