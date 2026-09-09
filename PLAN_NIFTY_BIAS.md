# PLAN — Nifty Bias Dashboard (OpenAlgo-native)

Step 1 of the build order in `nifty_dashboard_prompt_openalgo.md`.
**Nothing has been coded yet.** This plan is for review before any code lands.

---

## 0. The one deliberate deviation from the spec

The spec's §Architecture decision says *"do NOT fork or modify the OpenAlgo repo"* —
build a separate Streamlit project that talks to OpenAlgo over REST.

Your chat instruction is the opposite: *"an additional option under Tools"*, inside
`D:\Nordem\openalgo`, *"open for any kind of changes ... as per openalgo"*.

**Resolution taken here:** keep the spec's *signal set, weights and scoring model*
verbatim; replace its *packaging* with an OpenAlgo-native tool. Everything below
follows from that. If you actually want the standalone Streamlit repo, stop me now —
it is a completely different build.

### Spec → OpenAlgo-native mapping

| Spec says | Built as | Why |
| --- | --- | --- |
| Separate repo `nifty-bias-dashboard` | Tool #16 in this fork | You asked for it under Tools |
| Streamlit dark dashboard | React 19 + shadcn/ui page | Matches the other 15 tools; Streamlit cannot mount inside Flask |
| `openalgo` SDK over `http://127.0.0.1:5000` | Direct calls to the internal service layer | Removes an HTTP hop + API key; same functions the REST layer wraps |
| `data/snapshots.db` ad-hoc SQLite | 7th DB via `database/engine_factory.py` | Repo mandates NullPool engines (CLAUDE.md) |
| `run_collector.py` 60s loop | APScheduler job | `historify_scheduler_service.py` / `flow_scheduler_service.py` already share an instance |
| `config/weights.yaml` hot-reload | Weights in DB + sidebar sliders | No YAML watcher under gunicorn/eventlet; UI is live anyway |
| `run_dashboard.py` standalone | Page reads last snapshot from DB | Same stale-data banner behaviour |
| `MockProvider` + fixtures | **Kept as specified** | Lets the UI be built before market hours |
| `backtest/evaluate.py` | Service + a UI panel | Same per-signal hit-rate output |

---

## 1. Verified against the codebase (not assumed)

> Re-verified on `9117eb9cd` after the 902-commit update. Two items that existed on
> `feat/option-chain-greeks` turned out to be **your own work, not upstream's** —
> corrected below. Latest main also moved to **react-router v7** (`react-router`,
> not `react-router-dom`) and now ships **19** tools.

| Need | Status |
| --- | --- |
| Option chain | ✅ `services/option_chain_service.py` |
| Greeks / IV | ✅ `services/option_greeks_service.py` — **no local Black-Scholes needed** |
| Historical OI for N-min-ago deltas | 🔴 **NOT on main** — `option_chain_history_service.py` was your own work on `feat/option-chain-greeks`. Falls back to the spec's snapshot-diff approach, or cherry-pick your service. |
| Spot + India VIX quotes | ✅ `services/quotes_service.py`; Arrow supports `NSE_INDEX` (`broker/arrow/plugin.json`) |
| 5-min candles | ✅ `services/history_service.py` |
| Live ticks | ✅ Arrow **does** have a WS adapter (`broker/arrow/streaming/arrow_adapter.py`) |
| Indicators (EMA/RSI/Supertrend/VWAP) | ✅ `openalgo==2.0.2` SDK already a dependency — Rust-backed, no TA-Lib |
| Scheduler | ✅ APScheduler already in use |
| OI buildup classifier | 🔴 **NOT on main** — `classifyOiTrend()` is your own work. Will be **ported to Python** from `feat/option-chain-greeks`. |

---

## 2. Files to be created

```
blueprints/nifty_bias.py              # page route + JSON endpoints
services/nifty_bias_service.py        # orchestration: gather → score → persist
services/nifty_bias/                  # one module per signal (spec §3)
  ├── option_chain_signals.py         # A — 35%
  ├── technical_signals.py            # B — 25%
  ├── volatility_signals.py           # C — 15%
  ├── global_signals.py               # D — 25%
  └── scorer.py                       # weighted −1..+1 → P(up)
database/nifty_bias_db.py             # snapshots + weights (NullPool)
frontend/src/pages/NiftyBias.tsx      # the dashboard
frontend/src/api/nifty-bias.ts        # TanStack Query client
scripts/probe_chain.py                # spec step 2 — learn real shapes
test/test_nifty_bias_signals.py       # per-signal unit tests
docs/openalgo_notes.md                # field shapes as discovered
```

Edits to existing files (small, additive):
`app.py` (register blueprint) · `frontend/src/App.tsx` (route) ·
**`frontend/src/lib/tools.ts`** (card — the registry moved here on latest main;
`Tools.tsx` now just renders it) · `frontend/src/hooks/usePageTitle.ts`

---

## 3. Signals — kept exactly at the spec's weights

**A. Option chain — 35%** · PCR (OI + volume, ATM±5) · max pain & spot distance ·
highest call-OI = resistance / highest put-OI = support, spot position in band ·
OI change 15/30/60 min classified LB/SB/SC/LU · IV skew (ATM put IV − call IV) ·
ATM straddle premium + intraday trend.

**B. Technicals — 25%** · vs VWAP, EMA20 (5m), 50/200 DMA · RSI(14) 5m + daily ·
Supertrend(10,3) 5m · opening-range (first 15m) breakout · distance from PDH/PDL/PDC.

**C. Volatility — 15%** · India VIX level, 30-day percentile, intraday change ·
realised vs implied vol gap.

**D. Global / flows — 25%** · yfinance (S&P/Dow/Nasdaq futures, DXY, US 10Y, Brent,
GIFT Nifty) on a 2-min cache · previous-session FII/DII net cash.

Each returns `SignalResult(name, score −1..+1, weight, explanation)`.

---

## 4. Risks I want on the record

1. **🔴 FII/DII data has no source.** Not in OpenAlgo, not in yfinance. Needs an
   NSE/Moneycontrol scrape or manual entry. Until resolved, this sub-signal returns
   neutral and D's weight redistributes. **This is the one genuine spec gap.**
2. **eventlet forbids asyncio** (CLAUDE.md). yfinance is `requests`-based — must run
   on a real OS thread, never `asyncio.run()`, or it breaks in production but works
   on your Windows dev server. Pattern to copy: `telegram_bot_service.py:_render_plotly_png`.
3. **GIFT Nifty** via OpenAlgo is Zerodha-only (`GLOBAL_INDEX`); on Arrow it must come
   from yfinance.
4. **FD hygiene** (CLAUDE.md): the 60s collector adds a DB session + HTTP client +
   scheduler thread. All via `engine_factory` / shared `httpx_client` / module-level
   singletons, with an FD audit before I call it done.
5. **Backtest honesty:** P(up) is calibrated on your own recorded snapshots only —
   it will look good in-sample. `backtest/evaluate.py` reports per-signal hit rate so
   you can see which signals actually carry edge.

---

## 5. Two decisions I need from you

**a) Base branch — DONE.** Local `main` fast-forwarded 902 commits to
`upstream/main` @ `9117eb9cd` (2026-09-08); now building on
`feat/nifty-bias-dashboard` off that. `feat/option-chain-greeks` is untouched.

**b) Expiry.** Spec leaves `<<weekly / monthly>>` unfilled. I will default to
**weekly** unless you say otherwise.

---

## 6. Build order (spec §6 — stopping after each step)

1. **PLAN.md + skeleton** ← you are here
2. `scripts/probe_chain.py` → real field shapes → `docs/openalgo_notes.md`
3. MockProvider + full React UI on fixtures, end to end
4. Real provider wired to the internal services
5. Signals one at a time, each with unit tests
6. Backtest module
