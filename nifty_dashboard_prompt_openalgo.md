# Prompt for Claude (VS Code) — Nifty Bias Dashboard on OpenAlgo

Fill the `<<...>>` placeholders, then paste everything below the line.

---

Build me a complete, runnable Python project called **nifty-bias-dashboard**: a real-time dashboard that estimates the probability of Nifty 50 moving up or down over the next 1h / 3h / rest of session, using live option chain data plus technicals, volatility and global cues.

**Architecture decision (do not deviate):** I already run **OpenAlgo** (self-hosted, open source, Flask, at `http://127.0.0.1:5000`) connected to my **Arrow** broker account. OpenAlgo is my data and broker layer. Do NOT write any Arrow-specific code and do NOT fork or modify the OpenAlgo repo. My project is a separate repo that consumes OpenAlgo's local REST API, WebSocket feed and Python SDK.

Before writing code, read the docs at https://docs.openalgo.in/ — specifically the Python SDK pages (option chain, history, quotes, websockets), the API v1 reference, and the Python Strategy Host page — and confirm the exact request/response shapes. Ask me before guessing at a field name. Write your findings into `docs/openalgo_notes.md` as you go.

My OpenAlgo details: host `<<http://127.0.0.1:5000>>`, API key in `.env` as `OPENALGO_API_KEY`. Underlying: NIFTY, exchange `NSE_INDEX`. Expiry: `<<weekly / monthly>>`.

## 1. Data layer

Install the `openalgo` Python library. Wrap everything behind a `DataProvider` protocol with two implementations:

- `OpenAlgoProvider` — the real one:
  - `client.optionchain(underlying="NIFTY", exchange="NSE_INDEX", expiry_date=..., strike_count=<<15>>)` for the chain
  - quotes / LTP for NIFTY spot and INDIA VIX
  - `client.history(...)` for 5-min and daily candles (last 60 days)
  - OpenAlgo WebSocket (port 8765) for live ticks on spot and ATM ±5 strikes; fall back to REST polling if the socket drops
- `MockProvider` — replays a saved JSON snapshot from `data/fixtures/` so I can develop and run tests outside market hours with no broker session.

**Important:** OpenAlgo's option chain coverage depends on the underlying broker's entitlement. On first run, write a `scripts/probe_chain.py` that dumps one raw option chain response to `data/fixtures/chain_sample.json` and prints which fields are actually present (OI, OI change, volume, IV, greeks, bid/ask). Build the signal layer only on fields that exist. If IV is missing, compute it locally with Black-Scholes (Newton-Raphson on LTP, use the NSE risk-free rate from config), and if OI-change is missing, derive it by diffing consecutive snapshots stored in the DB.

Use OpenAlgo's built-in indicator functions (the Rust-backed ones in the `openalgo` library) for EMA/RSI/Supertrend/VWAP rather than reimplementing or pulling in TA-Lib.

For global cues, OpenAlgo won't help — use `yfinance` with a 2-minute cache for S&P/Dow/Nasdaq futures, DXY, US 10Y, Brent, and GIFT Nifty if available.

## 2. Signals (each returns a score in −1..+1 with an explanation string)

**A. Option chain — 35%**
- PCR by OI (total and ATM ±5) and PCR by volume
- Max pain strike, and spot's distance from it
- Highest call OI strike (resistance), highest put OI strike (support), spot's position in that band
- OI change over 15/30/60 min at ATM ±5: classify CE and PE as long buildup / short buildup / short covering / long unwinding
- IV skew: ATM put IV − ATM call IV
- ATM straddle premium and its intraday trend

**B. Technicals — 25%:** price vs VWAP, vs EMA20 (5m), vs 50/200 DMA; RSI(14) on 5m and daily; Supertrend(10,3) on 5m; opening-range (first 15 min) breakout status; distance from previous day high/low/close.

**C. Volatility — 15%:** India VIX level, its 30-day percentile, intraday change; realised vs implied vol gap.

**D. Global / flows — 25%:** the yfinance instruments above, plus previous session FII/DII net cash as a slow-moving carry signal.

## 3. Scoring engine

- `signals/` package, one module per signal, each exposing `compute(ctx) -> SignalResult(name, score, weight, explanation, raw)`.
- `engine/scorer.py` produces a weighted composite in −1..+1, then maps to `P(up)` with a logistic, and a label: Strong Bearish / Bearish / Neutral / Bullish / Strong Bullish.
- Weights in `config/weights.yaml`, hot-reloadable.
- Every snapshot (all raw values + composite + spot + timestamp) logged to SQLite at `data/snapshots.db`.
- `backtest/evaluate.py`: joins each snapshot with the realised Nifty move 1h / 3h / EOD later; reports hit rate, Brier score, per-signal correlation with realised move, and a grid search suggesting better weights. This is the part that tells me which signals actually have edge — make its output readable.

## 4. Two runnable entry points

- `run_collector.py` — headless fetch + score loop, every 60s, market-hours aware (09:15–15:30 IST, NSE holiday list in config), writes to SQLite. Must be written so I can also paste it into **OpenAlgo's Python Strategy Host** (`/python` page) and have it scheduled there: no CLI args required, config from env, graceful shutdown on SIGTERM, all output via `print`/logging so OpenAlgo's live log view is useful.
- `run_dashboard.py` — Streamlit app that only reads SQLite and the latest snapshot. It must work even if the collector is down (show a stale-data banner).

## 5. Dashboard layout (Streamlit, dark, auto-refresh 60s)

1. Header: spot, change, VIX, timestamp, market status, data-health (live/mock, last tick age, collector alive?)
2. Big gauge: `P(up)` + label + a plain-English paragraph naming the top 3 contributing signals
3. Horizontal bar chart of each signal's weighted contribution (red/green)
4. Option chain panel: CE vs PE OI bars per strike, max pain line, support/resistance highlighted, OI-change table for ATM ±5, PCR values, straddle premium trend
5. Plotly 5-min candles with VWAP, EMA20, Supertrend, ORB levels, and OI-derived support/resistance as horizontal lines
6. Global cues table, colour coded
7. Intraday line of `P(up)` overlaid on spot (from SQLite) so I can see whether the score led the move
8. Sidebar: weight sliders, refresh interval, expiry selector, live/mock toggle

## 6. Engineering requirements

- Python 3.11+, `requirements.txt`, `README.md` covering: OpenAlgo setup and Arrow connection, running the probe script, mock mode first, then live, then hosting the collector inside OpenAlgo.
- Structure: `providers/ signals/ engine/ dashboard/ backtest/ scripts/ config/ data/ tests/ docs/`
- Type hints, pydantic models, rotating file logs, IST everywhere.
- `pytest` for every signal against fixture data; integration test on MockProvider.
- A failing signal must never kill the loop — mark it stale, drop it from the composite, surface a warning in the UI.
- Clear on-screen disclaimer: statistical bias estimate, not a prediction or trading advice. No order placement code anywhere in this project.

**Order of work:** (1) `PLAN.md` + skeleton, (2) `scripts/probe_chain.py` so we learn the real data shape, (3) MockProvider + Streamlit UI end-to-end with fake data, (4) real OpenAlgoProvider, (5) signals one at a time with tests, (6) backtest module. Stop and show me the result after each step.
