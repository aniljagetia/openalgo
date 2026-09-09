import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

interface Signal {
  name: string
  group: string
  score: number | null
  weight: number
  explanation: string
  resolved: boolean
}

interface Group {
  key: string
  label: string
  weight: number
  score: number | null
  contribution: number
  resolved_fraction: number
  signals: Signal[]
}

interface ChainRow {
  strike: number
  ce_oi: number | null
  pe_oi: number | null
}

interface BiasResponse {
  status: string
  as_of: string
  market_status: string
  source: string
  errors: string[]
  spot: { ltp: number | null; prev_close: number | null; change_pct: number | null }
  vix: { ltp: number | null; prev_close: number | null }
  expiry: string | null
  atm_strike: number | null
  probability_up: number
  composite_score: number
  confidence: number
  label: string
  narrative: string
  groups: Group[]
  levels: Record<string, number | null>
  chain: ChainRow[]
}

const num = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? '--' : v.toLocaleString('en-IN', { maximumFractionDigits: digits })

/** Green above neutral, red below -- the single colour rule for the whole page. */
const toneFor = (score: number | null) => {
  if (score === null) return 'text-muted-foreground'
  if (score > 0.15) return 'text-emerald-500'
  if (score < -0.15) return 'text-red-500'
  return 'text-muted-foreground'
}

/**
 * Probability dial. A semicircular arc rather than a full gauge, because the
 * value is bounded 0-100 and the midpoint (50 = no edge) must be visually
 * obvious rather than inferred.
 */
function ProbabilityDial({ probability, label }: { probability: number; label: string }) {
  const pct = Math.round(probability * 100)
  const angle = Math.PI * (1 - probability)
  const r = 90
  const x = 100 + r * Math.cos(angle)
  const y = 100 - r * Math.sin(angle)
  const stroke = pct > 55 ? '#10b981' : pct < 45 ? '#ef4444' : '#94a3b8'

  return (
    <div className="flex flex-col items-center">
      <svg viewBox="0 0 200 115" className="w-full max-w-[280px]" role="img" aria-label={label}>
        <path
          d="M 10 100 A 90 90 0 0 1 190 100"
          fill="none"
          stroke="currentColor"
          className="text-muted/25"
          strokeWidth="14"
          strokeLinecap="round"
        />
        <path
          d={`M 10 100 A 90 90 0 0 1 ${x} ${y}`}
          fill="none"
          stroke={stroke}
          strokeWidth="14"
          strokeLinecap="round"
        />
        <line x1="100" y1="12" x2="100" y2="26" stroke="currentColor" className="text-muted-foreground" strokeWidth="2" />
      </svg>
      <div className="-mt-6 text-center">
        <div className="text-4xl font-bold tabular-nums" style={{ color: stroke }}>
          {pct}%
        </div>
        <div className="text-sm text-muted-foreground">probability Nifty closes higher</div>
        <div className="mt-1 text-lg font-semibold">{label}</div>
      </div>
    </div>
  )
}

/** Signed horizontal bar, centred on zero so direction reads at a glance. */
function ContributionBar({ value, max }: { value: number; max: number }) {
  const width = max > 0 ? (Math.abs(value) / max) * 50 : 0
  const positive = value >= 0
  return (
    <div className="relative h-4 w-full rounded bg-muted/30">
      <div className="absolute left-1/2 top-0 h-full w-px bg-border" />
      <div
        className={`absolute top-0 h-full ${positive ? 'bg-emerald-500' : 'bg-red-500'} rounded`}
        style={{
          width: `${width}%`,
          left: positive ? '50%' : `${50 - width}%`,
        }}
      />
    </div>
  )
}

export default function NiftyBias() {
  const [mock, setMock] = useState(false)

  const { data, isLoading, isFetching, error, refetch } = useQuery<BiasResponse>({
    queryKey: ['nifty-bias', mock],
    queryFn: async () => {
      const res = await fetch(`/niftybias/api/bias${mock ? '?mock=1' : ''}`, {
        credentials: 'include',
      })
      if (!res.ok) throw new Error(`Request failed: ${res.status}`)
      return res.json()
    },
    refetchInterval: 60_000,
  })

  if (isLoading) {
    return <div className="py-10 text-center text-muted-foreground">Loading Nifty bias...</div>
  }

  if (error || !data || data.status !== 'success') {
    return (
      <Card className="mt-6">
        <CardContent className="py-8 text-center">
          <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-amber-500" />
          <p className="font-medium">Could not load the bias reading.</p>
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

  const maxContribution = Math.max(...data.groups.map((g) => Math.abs(g.contribution)), 0.01)
  const maxOi = Math.max(...data.chain.map((r) => Math.max(r.ce_oi ?? 0, r.pe_oi ?? 0)), 1)
  const changePct = data.spot.change_pct ?? 0

  return (
    <div className="space-y-6 py-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Nifty Bias Dashboard</h1>
          <p className="mt-1 text-muted-foreground">
            Directional bias scored from option chain, technicals, volatility and global cues
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

      {/* Data health */}
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
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card>
          <CardContent className="py-4">
            <div className="text-sm text-muted-foreground">NIFTY</div>
            <div className="text-2xl font-bold tabular-nums">{num(data.spot.ltp, 1)}</div>
            <div className={`text-sm tabular-nums ${changePct >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
              {changePct >= 0 ? '+' : ''}
              {changePct.toFixed(2)}%
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
            <div className="text-sm text-muted-foreground">Model confidence</div>
            <div className="text-2xl font-bold tabular-nums">
              {Math.round(data.confidence * 100)}%
            </div>
            <div className="text-xs text-muted-foreground">of intended inputs resolved</div>
          </CardContent>
        </Card>
      </div>

      {/* Dial + narrative */}
      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardContent className="py-6">
            <ProbabilityDial probability={data.probability_up} label={data.label} />
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="text-base">What is driving this</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm leading-relaxed text-muted-foreground">{data.narrative}</p>
            <div className="space-y-3">
              {data.groups.map((g) => (
                <div key={g.key} className="space-y-1">
                  <div className="flex justify-between text-sm">
                    <span className="font-medium">
                      {g.label}{' '}
                      <span className="text-muted-foreground">
                        ({Math.round(g.weight * 100)}%)
                      </span>
                    </span>
                    <span className={`tabular-nums ${toneFor(g.score)}`}>
                      {g.score === null ? 'no data' : g.contribution.toFixed(3)}
                    </span>
                  </div>
                  <ContributionBar value={g.contribution} max={maxContribution} />
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Signals */}
      <div className="grid gap-6 md:grid-cols-2">
        {data.groups.map((g) => (
          <Card key={g.key}>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-base">
                <span>{g.label}</span>
                <Badge variant="outline">
                  {Math.round(g.resolved_fraction * 100)}% resolved
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {g.signals.map((s) => (
                <div key={s.name} className="border-b border-border/50 pb-2 last:border-0">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-medium">{s.name}</span>
                    <span className={`text-sm tabular-nums ${toneFor(s.score)}`}>
                      {s.score === null ? '--' : s.score.toFixed(3)}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">{s.explanation}</p>
                </div>
              ))}
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Option chain OI */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Open Interest by strike
            <span className="ml-2 text-sm font-normal text-muted-foreground">
              red = call OI (resistance), green = put OI (support)
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-1">
            {data.chain.map((row) => {
              const ce = ((row.ce_oi ?? 0) / maxOi) * 100
              const pe = ((row.pe_oi ?? 0) / maxOi) * 100
              const isAtm = row.strike === data.atm_strike
              return (
                <div key={row.strike} className="flex items-center gap-2 text-xs">
                  <div className="flex h-4 w-1/2 justify-end">
                    <div className="h-full rounded-l bg-red-500/70" style={{ width: `${ce}%` }} />
                  </div>
                  <div
                    className={`w-20 shrink-0 text-center tabular-nums ${
                      isAtm ? 'font-bold text-primary' : 'text-muted-foreground'
                    }`}
                  >
                    {num(row.strike, 0)}
                  </div>
                  <div className="flex h-4 w-1/2">
                    <div className="h-full rounded-r bg-emerald-500/70" style={{ width: `${pe}%` }} />
                  </div>
                </div>
              )
            })}
          </div>
        </CardContent>
      </Card>

      <p className="text-center text-xs text-muted-foreground">
        Updated {new Date(data.as_of).toLocaleString('en-IN')} - refreshes every 60s
      </p>
    </div>
  )
}
