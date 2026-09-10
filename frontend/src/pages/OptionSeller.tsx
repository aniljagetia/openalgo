import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

interface Factor {
  name: string
  score: number | null
  weight: number
  explanation: string
  resolved?: boolean
  detail?: Record<string, number | string | boolean | null>
}

interface Leg {
  strike: number
  type: 'CE' | 'PE'
  symbol: string | null
  ltp: number
  oi: number | null
  volume: number | null
  iv: number | null
  delta: number | null
  theta: number | null
  distance: number | null
  distance_pct: number | null
  breakeven: number
  stop_loss: number
  prob_otm: number | null
  reason: string
  invalidation?: { label: string; price: number; distance: number; note: string } | null
  hedge?: Leg | null
  spread?: {
    buy_strike: number
    net_credit: number
    width: number
    max_loss: number
    risk_reward: number | null
  }
}

interface LevelRow {
  label: string
  kind: string
  price: number
  distance: number | null
  distance_pct: number | null
  side: string | null
}

interface SellerResponse {
  status: string
  as_of: string
  market_status: string
  source: string
  errors: string[]
  spot: { ltp: number | null; prev_close: number | null; change_pct: number | null }
  vix: { ltp: number | null; prev_close: number | null }
  expiry: string | null
  atm_strike: number | null
  levels: {
    prev_high: number | null
    prev_low: number | null
    prev_close: number | null
    prev_range: number | null
    today_open: number | null
    today_high: number | null
    today_low: number | null
    day_range: number | null
    typical_range: number | null
    range_vs_typical: number | null
    position_in_day?: number
    position_in_prev?: number
    prev_range_state?: string
    gap_pct?: number
    gap_filled?: boolean | null
  }
  level_rows: LevelRow[]
  side: {
    score: number | null
    confidence: number
    side: string
    strength: string
    factors: Factor[]
  }
  side_deltas: { label: string; delta: number | null }[]
  regime: {
    score: number | null
    raw_score?: number
    confidence: number
    verdict: string
    note: string
    gate_multiplier: number
    factors: Factor[]
  }
  plan: {
    action: string
    headline: string
    rationale: string
    legs: Leg[]
    confidence: number
  }
  call_candidates: Leg[]
  put_candidates: Leg[]
  timeframes: {
    label: string
    nifty_pct: number | null
    banknifty_pct: number | null
    direction: string | null
  }[]
  alignment: { state: string; score: number | null; note: string }
  events: { name: string; severity: string; kind: string; when: string; days_away: number }[]
}

const num = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? '--' : v.toLocaleString('en-IN', { maximumFractionDigits: digits })

const signed = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`

/**
 * Colour rule for this page differs from the bias dashboard on purpose. Here a
 * positive score means "sell puts", which is the bullish side, so green still
 * means up -- but the label the trader reads is the trade, not the direction.
 */
const sideTone = (score: number | null) => {
  if (score === null) return 'text-muted-foreground'
  if (score > 0.15) return 'text-emerald-500'
  if (score < -0.15) return 'text-red-500'
  return 'text-amber-500'
}

const ACTION_STYLES: Record<string, string> = {
  sell: 'border-emerald-500/50 bg-emerald-500/5',
  spread: 'border-amber-500/50 bg-amber-500/5',
  stand_aside: 'border-red-500/50 bg-red-500/5',
  wait: 'border-muted',
}

/**
 * The side meter. A single axis from "sell calls" to "sell puts" with a marker,
 * because the decision is one-dimensional and a dial would imply more precision
 * than seven weighted factors deserve.
 */
function SideMeter({ score }: { score: number | null }) {
  const pct = score === null ? 50 : ((score + 1) / 2) * 100
  return (
    <div className="space-y-2">
      <div className="flex justify-between text-xs font-medium">
        <span className="text-red-500">Sell calls</span>
        <span className="text-muted-foreground">Balanced</span>
        <span className="text-emerald-500">Sell puts</span>
      </div>
      <div className="relative h-3 rounded-full bg-gradient-to-r from-red-500/30 via-muted to-emerald-500/30">
        <div className="absolute inset-y-0 left-1/2 w-px bg-border" />
        {score !== null && (
          <div
            className="absolute -top-1 h-5 w-1.5 -translate-x-1/2 rounded-full bg-foreground"
            style={{ left: `${pct}%` }}
          />
        )}
      </div>
      <div className={`text-center text-sm tabular-nums ${sideTone(score)}`}>
        {score === null ? 'No read' : signed(score)}
      </div>
    </div>
  )
}

/**
 * Where spot sits between the day's low and high, with yesterday's range drawn
 * behind it. Seeing both at once is what tells a seller whether today is still
 * inside yesterday's decided range or has broken out of it.
 */
function RangeBar({ levels, spot }: { levels: SellerResponse['levels']; spot: number | null }) {
  const values = [
    levels.prev_high,
    levels.prev_low,
    levels.today_high,
    levels.today_low,
    spot,
  ].filter((v): v is number => v !== null && v !== undefined)
  if (values.length < 2) {
    return <p className="text-sm text-muted-foreground">Not enough session data yet.</p>
  }
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const span = hi - lo || 1
  const at = (v: number) => ((v - lo) / span) * 100

  return (
    <div className="space-y-3">
      <div className="relative h-16">
        {levels.prev_low !== null && levels.prev_high !== null && (
          <div
            className="absolute top-1 h-5 rounded bg-muted"
            style={{
              left: `${at(levels.prev_low)}%`,
              width: `${at(levels.prev_high) - at(levels.prev_low)}%`,
            }}
            title="Yesterday's range"
          />
        )}
        {levels.today_low !== null && levels.today_high !== null && (
          <div
            className="absolute top-9 h-5 rounded bg-sky-500/40"
            style={{
              left: `${at(levels.today_low)}%`,
              width: `${at(levels.today_high) - at(levels.today_low)}%`,
            }}
            title="Today's range"
          />
        )}
        {spot !== null && (
          <div
            className="absolute top-0 h-14 w-0.5 bg-foreground"
            style={{ left: `${at(spot)}%` }}
            title={`Spot ${spot}`}
          />
        )}
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <div>
          <div className="text-muted-foreground">Prev high</div>
          <div className="font-medium tabular-nums">{num(levels.prev_high, 1)}</div>
        </div>
        <div>
          <div className="text-muted-foreground">Prev low</div>
          <div className="font-medium tabular-nums">{num(levels.prev_low, 1)}</div>
        </div>
        <div>
          <div className="text-muted-foreground">Today high</div>
          <div className="font-medium tabular-nums">{num(levels.today_high, 1)}</div>
        </div>
        <div>
          <div className="text-muted-foreground">Today low</div>
          <div className="font-medium tabular-nums">{num(levels.today_low, 1)}</div>
        </div>
      </div>
    </div>
  )
}

/** One candidate or recommended short leg, with its exit stated before its entry. */
function LegCard({ leg }: { leg: Leg }) {
  const isCall = leg.type === 'CE'
  return (
    <Card className={isCall ? 'border-red-500/40' : 'border-emerald-500/40'}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-base">
          <span>
            SELL {num(leg.strike, 0)} {leg.type}
          </span>
          <Badge variant="outline" className="tabular-nums">
            {num(leg.ltp)} pts
          </Badge>
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          {leg.symbol ?? ''} &middot; chosen as {leg.reason}
        </p>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div>
            <div className="text-xs text-muted-foreground">Distance</div>
            <div className="tabular-nums">
              {signed(leg.distance, 0)} ({signed(leg.distance_pct, 2)}%)
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Delta</div>
            <div className="tabular-nums">{num(leg.delta, 3)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Est. P(expires OTM)</div>
            <div className="tabular-nums">
              {leg.prob_otm === null ? '--' : `${Math.round(leg.prob_otm * 100)}%`}
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">IV</div>
            <div className="tabular-nums">{num(leg.iv, 1)}%</div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 rounded-md bg-muted/40 p-3 sm:grid-cols-3">
          <div>
            <div className="text-xs text-muted-foreground">Breakeven</div>
            <div className="font-medium tabular-nums">{num(leg.breakeven, 1)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Premium stop (2x)</div>
            <div className="font-medium tabular-nums text-red-500">{num(leg.stop_loss)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Open interest</div>
            <div className="font-medium tabular-nums">{num(leg.oi, 0)}</div>
          </div>
        </div>

        {leg.invalidation && (
          <div className="rounded-md border border-amber-500/40 p-3 text-xs">
            <div className="font-medium">
              Level stop: {leg.invalidation.label} at {num(leg.invalidation.price, 1)} (
              {signed(leg.invalidation.distance, 0)} pts away)
            </div>
            <p className="mt-1 text-muted-foreground">{leg.invalidation.note}</p>
          </div>
        )}

        {leg.spread && leg.hedge && (
          <div className="rounded-md border p-3 text-xs">
            <div className="font-medium">
              Defined-risk version: buy {num(leg.spread.buy_strike, 0)} {leg.type} at{' '}
              {num(leg.hedge.ltp)}
            </div>
            <div className="mt-1 grid grid-cols-3 gap-2 text-muted-foreground">
              <span>
                Net credit <span className="tabular-nums">{num(leg.spread.net_credit)}</span>
              </span>
              <span>
                Max loss <span className="tabular-nums">{num(leg.spread.max_loss)}</span>
              </span>
              <span>
                Risk:reward{' '}
                <span className="tabular-nums">
                  {leg.spread.risk_reward === null ? '--' : `${num(leg.spread.risk_reward)}:1`}
                </span>
              </span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/** A 0-1 regime factor, drawn as a filled bar so weak days are visible at a glance. */
function RegimeRow({ factor }: { factor: Factor }) {
  const pct = factor.score === null ? 0 : factor.score * 100
  return (
    <div className="border-b py-2 last:border-0">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">{factor.name}</span>
        <span className="text-sm tabular-nums text-muted-foreground">
          {factor.score === null ? 'unresolved' : factor.score.toFixed(2)}
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded-full bg-muted">
        <div
          className={`h-1.5 rounded-full ${
            pct >= 65 ? 'bg-emerald-500' : pct >= 40 ? 'bg-amber-500' : 'bg-red-500'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{factor.explanation}</p>
    </div>
  )
}

export default function OptionSeller() {
  const [mock, setMock] = useState(false)

  const { data, isLoading, isFetching, error, refetch } = useQuery<SellerResponse>({
    queryKey: ['option-seller', mock],
    queryFn: async () => {
      const res = await fetch(`/optionseller/api/snapshot${mock ? '?mock=1' : ''}`, {
        credentials: 'include',
      })
      if (!res.ok) throw new Error(`Request failed: ${res.status}`)
      return res.json()
    },
    refetchInterval: 60_000,
  })

  if (isLoading) {
    return <div className="py-10 text-center text-muted-foreground">Loading seller view...</div>
  }

  if (error || !data || data.status !== 'success') {
    return (
      <Card className="mt-6">
        <CardContent className="py-8 text-center">
          <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-amber-500" />
          <p className="font-medium">Could not load the option-seller reading.</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {error instanceof Error ? error.message : 'The service returned an error.'}
          </p>
          <Button className="mt-4" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    )
  }

  const changePct = data.spot.change_pct ?? 0
  const straddle = data.regime.factors.find((f) => f.name === 'Straddle premium')
  const straddleTotal = (straddle?.detail?.total as number | undefined) ?? null
  const straddlePct = (straddle?.detail?.pct_of_spot as number | undefined) ?? null

  return (
    <div className="space-y-6 py-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Intraday Option Seller</h1>
          <p className="mt-1 text-muted-foreground">
            Sell calls or sell puts today, which strike, and whether the day is worth selling at
            all
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={data.source === 'live' ? 'default' : 'secondary'}>
            {data.source === 'live' ? 'Live' : 'Mock data'}
          </Badge>
          <Badge variant="outline">{data.market_status}</Badge>
          <Button variant="outline" size="sm" onClick={() => setMock((m) => !m)}>
            {mock ? 'Use live' : 'Use fixtures'}
          </Button>
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </div>

      {data.errors.length > 0 && (
        <Card className="border-amber-500/40">
          <CardContent className="flex gap-3 py-3 text-sm">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
            <ul className="space-y-1 text-muted-foreground">
              {data.errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* Top strip */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Card>
          <CardContent className="py-4">
            <div className="text-sm text-muted-foreground">NIFTY</div>
            <div className="text-2xl font-bold tabular-nums">{num(data.spot.ltp, 1)}</div>
            <div
              className={`text-sm tabular-nums ${changePct >= 0 ? 'text-emerald-500' : 'text-red-500'}`}
            >
              {signed(changePct)}%
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="py-4">
            <div className="text-sm text-muted-foreground">India VIX</div>
            <div className="text-2xl font-bold tabular-nums">{num(data.vix.ltp)}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="py-4">
            <div className="text-sm text-muted-foreground">Expiry / ATM</div>
            <div className="text-lg font-semibold">{data.expiry ?? '--'}</div>
            <div className="text-sm text-muted-foreground tabular-nums">
              ATM {num(data.atm_strike, 0)}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="py-4">
            <div className="text-sm text-muted-foreground">ATM straddle</div>
            <div className="text-2xl font-bold tabular-nums">{num(straddleTotal, 1)}</div>
            <div className="text-xs text-muted-foreground">
              {straddlePct === null ? 'unavailable' : `${straddlePct.toFixed(2)}% implied range`}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="py-4">
            <div className="text-sm text-muted-foreground">Day range</div>
            <div className="text-2xl font-bold tabular-nums">{num(data.levels.day_range, 0)}</div>
            <div className="text-xs text-muted-foreground">
              {data.levels.range_vs_typical === null
                ? 'no baseline yet'
                : `${data.levels.range_vs_typical.toFixed(2)}x a normal day`}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* The decision */}
      <Card className={ACTION_STYLES[data.plan.action] ?? 'border-muted'}>
        <CardHeader className="pb-3">
          <CardTitle className="flex flex-wrap items-center justify-between gap-2">
            <span>{data.plan.headline}</span>
            <Badge variant="outline">{data.regime.verdict} regime</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-6 md:grid-cols-2">
          <div className="space-y-4">
            <SideMeter score={data.side.score} />
            <div className="flex flex-wrap gap-3 text-xs">
              {data.side_deltas.map((d) => (
                <span key={d.label} className="rounded bg-muted px-2 py-1 tabular-nums">
                  {d.label}{' '}
                  <span className={sideTone(d.delta)}>
                    {d.delta === null ? '--' : signed(d.delta, 2)}
                  </span>
                </span>
              ))}
            </div>
            <p className="text-sm text-muted-foreground">{data.plan.rationale}</p>
            <div className="text-xs text-muted-foreground">
              Side confidence {Math.round(data.side.confidence * 100)}% &middot; regime confidence{' '}
              {Math.round(data.regime.confidence * 100)}%
            </div>
          </div>
          <div className="space-y-2">
            <div className="text-sm font-medium">Sellability {num(data.regime.score, 2)}</div>
            <div className="h-2 w-full rounded-full bg-muted">
              <div
                className={`h-2 rounded-full ${
                  (data.regime.score ?? 0) >= 0.65
                    ? 'bg-emerald-500'
                    : (data.regime.score ?? 0) >= 0.45
                      ? 'bg-amber-500'
                      : 'bg-red-500'
                }`}
                style={{ width: `${(data.regime.score ?? 0) * 100}%` }}
              />
            </div>
            <p className="text-sm text-muted-foreground">{data.regime.note}</p>
            {data.regime.gate_multiplier < 1 && (
              <p className="text-xs text-amber-500">
                An event is pending; the regime score has been damped by{' '}
                {Math.round((1 - data.regime.gate_multiplier) * 100)}%.
              </p>
            )}
          </div>
        </CardContent>
      </Card>

      {/* The trade */}
      {data.plan.legs.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          {data.plan.legs.map((leg) => (
            <LegCard key={`${leg.type}-${leg.strike}`} leg={leg} />
          ))}
        </div>
      )}

      {/* Levels */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Yesterday and today</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <RangeBar levels={data.levels} spot={data.spot.ltp} />
            <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
              <div>
                <div className="text-muted-foreground">Prev close</div>
                <div className="font-medium tabular-nums">{num(data.levels.prev_close, 1)}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Today open</div>
                <div className="font-medium tabular-nums">{num(data.levels.today_open, 1)}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Gap</div>
                <div className="font-medium tabular-nums">
                  {data.levels.gap_pct === undefined ? '--' : `${signed(data.levels.gap_pct)}%`}
                  {data.levels.gap_filled ? ' (filled)' : ''}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground">Vs prev range</div>
                <div className="font-medium">{data.levels.prev_range_state ?? '--'}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Position in day</div>
                <div className="font-medium tabular-nums">
                  {data.levels.position_in_day === undefined
                    ? '--'
                    : `${Math.round(data.levels.position_in_day * 100)}%`}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground">Typical range</div>
                <div className="font-medium tabular-nums">
                  {num(data.levels.typical_range, 0)}
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Levels, nearest first</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="max-h-[360px] space-y-1 overflow-y-auto text-sm">
              {data.level_rows.map((row) => (
                <div
                  key={`${row.label}-${row.price}`}
                  className={`grid grid-cols-12 gap-2 rounded px-2 py-1 ${
                    row.side === 'above' ? 'bg-red-500/5' : 'bg-emerald-500/5'
                  }`}
                >
                  <span className="col-span-5 truncate">{row.label}</span>
                  <span className="col-span-3 text-right tabular-nums">{num(row.price, 1)}</span>
                  <span className="col-span-2 text-right tabular-nums text-muted-foreground">
                    {signed(row.distance, 0)}
                  </span>
                  <span className="col-span-2 text-right tabular-nums text-muted-foreground">
                    {signed(row.distance_pct, 2)}%
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Why this side */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Which side, and why</CardTitle>
          </CardHeader>
          <CardContent>
            {data.side.factors.map((f) => (
              <div key={f.name} className="border-b py-2 last:border-0">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium">{f.name}</span>
                  <span className={`text-sm tabular-nums ${sideTone(f.score)}`}>
                    {f.score === null ? 'unresolved' : signed(f.score)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{f.explanation}</p>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Is it a day to sell?</CardTitle>
          </CardHeader>
          <CardContent>
            {data.regime.factors.map((f) => (
              <RegimeRow key={f.name} factor={f} />
            ))}
          </CardContent>
        </Card>
      </div>

      {/* Momentum + candidates */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Momentum now</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {data.timeframes.map((tf) => (
              <div key={tf.label} className="flex items-center justify-between">
                <span className="font-medium">{tf.label}</span>
                <span
                  className={`tabular-nums ${
                    tf.direction === 'up'
                      ? 'text-emerald-500'
                      : tf.direction === 'down'
                        ? 'text-red-500'
                        : 'text-muted-foreground'
                  }`}
                >
                  {signed(tf.nifty_pct, 2)}%
                </span>
              </div>
            ))}
            <p className="pt-2 text-xs text-muted-foreground">{data.alignment.note}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base text-red-500">Call strikes to sell</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {data.call_candidates.length === 0 && (
              <p className="text-muted-foreground">No priceable call strike.</p>
            )}
            {data.call_candidates.map((leg) => (
              <div key={leg.strike} className="border-b pb-2 last:border-0">
                <div className="flex justify-between">
                  <span className="font-medium tabular-nums">{num(leg.strike, 0)} CE</span>
                  <span className="tabular-nums">{num(leg.ltp)}</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {signed(leg.distance, 0)} pts away &middot; delta {num(leg.delta, 3)} &middot;{' '}
                  {leg.reason}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base text-emerald-500">Put strikes to sell</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {data.put_candidates.length === 0 && (
              <p className="text-muted-foreground">No priceable put strike.</p>
            )}
            {data.put_candidates.map((leg) => (
              <div key={leg.strike} className="border-b pb-2 last:border-0">
                <div className="flex justify-between">
                  <span className="font-medium tabular-nums">{num(leg.strike, 0)} PE</span>
                  <span className="tabular-nums">{num(leg.ltp)}</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {signed(leg.distance, 0)} pts away &middot; delta {num(leg.delta, 3)} &middot;{' '}
                  {leg.reason}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>

      {data.events.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Events ahead</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm">
            {data.events.map((e) => (
              <div key={`${e.name}-${e.when}`} className="flex justify-between">
                <span>{e.name}</span>
                <span className="text-muted-foreground">
                  {e.when} &middot; {e.days_away}d &middot; {e.severity}
                </span>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <p className="text-xs text-muted-foreground">
        Not advice. Probabilities are the option market&apos;s own estimate via delta, and the
        1/3/5/15-minute change columns only fill while this page is open -- they reset whenever the
        server restarts.
      </p>
    </div>
  )
}
