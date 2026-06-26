export interface OptionChainResponse {
  status: 'success' | 'error'
  underlying: string
  underlying_ltp: number
  underlying_prev_close: number
  expiry_date: string
  atm_strike: number
  chain: OptionStrike[]
  message?: string
}

export interface OptionStrike {
  strike: number
  ce: OptionData | null
  pe: OptionData | null
}

export interface OptionData {
  symbol: string
  label: string
  ltp: number
  bid: number
  ask: number
  bid_qty: number
  ask_qty: number
  open: number
  high: number
  low: number
  prev_close: number
  volume: number
  oi: number
  // Previous-day close OI. Used by the FE to derive an OI Trend
  // (Long Buildup / Short Buildup / Long Unwinding / Short Covering)
  // when paired with ltp + prev_close. May be null when the broker
  // doesn't return prev_oi or oi_change — FE renders the trend as '-'
  // in that case.
  prev_oi?: number | null
  lotsize: number
  tick_size: number
  // Black-76 derived fields (optional — backend may return null when IV
  // can't converge, opengreeks is missing, or the leg has no quote).
  // `iv` is a percentage (e.g. 18.4 = 18.4%).
  iv?: number | null
  delta?: number | null
  gamma?: number | null
  theta?: number | null
  vega?: number | null
}

export interface OptionChainParams {
  underlying: string
  exchange: string
  expiry_date: string
  strike_count?: number
}

export interface MarketMetrics {
  total_ce_oi: number
  total_pe_oi: number
  total_ce_volume: number
  total_pe_volume: number
  pcr: number
}

export interface OptionChainState {
  data: OptionChainResponse | null
  isLoading: boolean
  isConnected: boolean
  error: string | null
  lastUpdate: Date | null
}

// Column configuration types
export type ColumnKey =
  | 'ce_oi'
  | 'ce_volume'
  | 'ce_oi_trend'
  | 'ce_bias'
  | 'ce_iv'
  | 'ce_delta'
  | 'ce_gamma'
  | 'ce_theta'
  | 'ce_vega'
  | 'ce_bid_qty'
  | 'ce_bid'
  | 'ce_ltp'
  | 'ce_ask'
  | 'ce_ask_qty'
  | 'ce_spread'
  | 'strike'
  | 'pe_spread'
  | 'pe_ask_qty'
  | 'pe_ask'
  | 'pe_ltp'
  | 'pe_bid'
  | 'pe_bid_qty'
  | 'pe_bias'
  | 'pe_oi_trend'
  | 'pe_vega'
  | 'pe_theta'
  | 'pe_gamma'
  | 'pe_delta'
  | 'pe_iv'
  | 'pe_volume'
  | 'pe_oi'

export type ColumnSide = 'ce' | 'pe' | 'center'

export interface ColumnDefinition {
  key: ColumnKey
  label: string
  side: ColumnSide
  width: string
  align: 'left' | 'center' | 'right'
  defaultVisible: boolean
  formatter?:
    | 'number'
    | 'price'
    | 'spread'
    | 'greek'
    | 'iv'
    | 'oi_trend'
    | 'bias'
    | 'none'
}

export const COLUMN_DEFINITIONS: ColumnDefinition[] = [
  // CE columns (left side) - ordered left to right.
  // Greeks sit at the very LEFT (top-left of the CE half) per user
  // request — IV/Delta/Gamma/Theta/Vega first, then OI/Volume/quote
  // columns, with Spread closest to the strike.
  {
    key: 'ce_iv',
    label: 'IV',
    side: 'ce',
    width: 'w-14',
    align: 'right',
    defaultVisible: true,
    formatter: 'iv',
  },
  {
    key: 'ce_delta',
    label: 'Delta',
    side: 'ce',
    width: 'w-14',
    align: 'right',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'ce_gamma',
    label: 'Gamma',
    side: 'ce',
    width: 'w-16',
    align: 'right',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'ce_theta',
    label: 'Theta',
    side: 'ce',
    width: 'w-14',
    align: 'right',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'ce_vega',
    label: 'Vega',
    side: 'ce',
    width: 'w-14',
    align: 'right',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'ce_oi',
    label: 'OI',
    side: 'ce',
    width: 'w-20',
    align: 'right',
    defaultVisible: true,
    formatter: 'number',
  },
  {
    key: 'ce_volume',
    label: 'Volume',
    side: 'ce',
    width: 'w-20',
    align: 'right',
    defaultVisible: true,
    formatter: 'number',
  },
  // OI Trend = price-direction × OI-direction matrix. Visually sits
  // next to OI/Volume since it's derived from them. Bias is the
  // bullish/bearish summary tag.
  {
    key: 'ce_oi_trend',
    label: 'OI Trend',
    side: 'ce',
    width: 'w-28',
    align: 'right',
    defaultVisible: true,
    formatter: 'oi_trend',
  },
  {
    key: 'ce_bias',
    label: 'Bias',
    side: 'ce',
    width: 'w-20',
    align: 'right',
    defaultVisible: true,
    formatter: 'bias',
  },
  {
    key: 'ce_bid_qty',
    label: 'Bid Qty',
    side: 'ce',
    width: 'w-16',
    align: 'right',
    defaultVisible: true,
    formatter: 'number',
  },
  {
    key: 'ce_bid',
    label: 'Bid',
    side: 'ce',
    width: 'w-16',
    align: 'right',
    defaultVisible: true,
    formatter: 'price',
  },
  {
    key: 'ce_ltp',
    label: 'LTP',
    side: 'ce',
    width: 'w-16',
    align: 'right',
    defaultVisible: true,
    formatter: 'price',
  },
  {
    key: 'ce_ask',
    label: 'Ask',
    side: 'ce',
    width: 'w-16',
    align: 'right',
    defaultVisible: true,
    formatter: 'price',
  },
  {
    key: 'ce_ask_qty',
    label: 'Ask Qty',
    side: 'ce',
    width: 'w-16',
    align: 'right',
    defaultVisible: true,
    formatter: 'number',
  },
  {
    key: 'ce_spread',
    label: 'Spread',
    side: 'ce',
    width: 'w-14',
    align: 'right',
    defaultVisible: true,
    formatter: 'spread',
  },
  // Center column
  {
    key: 'strike',
    label: 'Strike',
    side: 'center',
    width: 'w-20',
    align: 'center',
    defaultVisible: true,
    formatter: 'none',
  },
  // PE columns (right side) - ordered left to right
  {
    key: 'pe_spread',
    label: 'Spread',
    side: 'pe',
    width: 'w-14',
    align: 'left',
    defaultVisible: true,
    formatter: 'spread',
  },
  {
    key: 'pe_ask_qty',
    label: 'Ask Qty',
    side: 'pe',
    width: 'w-16',
    align: 'left',
    defaultVisible: true,
    formatter: 'number',
  },
  {
    key: 'pe_ask',
    label: 'Ask',
    side: 'pe',
    width: 'w-16',
    align: 'left',
    defaultVisible: true,
    formatter: 'price',
  },
  {
    key: 'pe_ltp',
    label: 'LTP',
    side: 'pe',
    width: 'w-16',
    align: 'left',
    defaultVisible: true,
    formatter: 'price',
  },
  {
    key: 'pe_bid',
    label: 'Bid',
    side: 'pe',
    width: 'w-16',
    align: 'left',
    defaultVisible: true,
    formatter: 'price',
  },
  {
    key: 'pe_bid_qty',
    label: 'Bid Qty',
    side: 'pe',
    width: 'w-16',
    align: 'left',
    defaultVisible: true,
    formatter: 'number',
  },
  // Mirror of CE: Bias then OI Trend so left-to-right reads
  // Bid Qty → Bias → OI Trend → Volume → OI → Greeks.
  {
    key: 'pe_bias',
    label: 'Bias',
    side: 'pe',
    width: 'w-20',
    align: 'left',
    defaultVisible: true,
    formatter: 'bias',
  },
  {
    key: 'pe_oi_trend',
    label: 'OI Trend',
    side: 'pe',
    width: 'w-28',
    align: 'left',
    defaultVisible: true,
    formatter: 'oi_trend',
  },
  {
    key: 'pe_volume',
    label: 'Volume',
    side: 'pe',
    width: 'w-20',
    align: 'left',
    defaultVisible: true,
    formatter: 'number',
  },
  {
    key: 'pe_oi',
    label: 'OI',
    side: 'pe',
    width: 'w-20',
    align: 'left',
    defaultVisible: true,
    formatter: 'number',
  },
  // Black-76 Greeks pinned to the RIGHT edge of the PE half (the user's
  // "top right" target). Mirror of the CE order — Vega is closest to OI,
  // IV is the rightmost column on the page so the chain reads
  // symmetrically with IV bookending both halves.
  {
    key: 'pe_vega',
    label: 'Vega',
    side: 'pe',
    width: 'w-14',
    align: 'left',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'pe_theta',
    label: 'Theta',
    side: 'pe',
    width: 'w-14',
    align: 'left',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'pe_gamma',
    label: 'Gamma',
    side: 'pe',
    width: 'w-16',
    align: 'left',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'pe_delta',
    label: 'Delta',
    side: 'pe',
    width: 'w-14',
    align: 'left',
    defaultVisible: true,
    formatter: 'greek',
  },
  {
    key: 'pe_iv',
    label: 'IV',
    side: 'pe',
    width: 'w-14',
    align: 'left',
    defaultVisible: true,
    formatter: 'iv',
  },
]

export const DEFAULT_COLUMN_ORDER: ColumnKey[] = COLUMN_DEFINITIONS.map((col) => col.key)

export const DEFAULT_VISIBLE_COLUMNS: ColumnKey[] = COLUMN_DEFINITIONS.filter(
  (col) => col.defaultVisible
).map((col) => col.key)

export type BarDataSource = 'oi' | 'volume'
export type BarStyle = 'gradient' | 'solid'

export interface OptionChainPreferences {
  visibleColumns: ColumnKey[]
  columnOrder: ColumnKey[]
  strikeCount: number
  selectedUnderlying: string
  barDataSource: BarDataSource
  barStyle: BarStyle
}

export const DEFAULT_PREFERENCES: OptionChainPreferences = {
  visibleColumns: DEFAULT_VISIBLE_COLUMNS,
  columnOrder: DEFAULT_COLUMN_ORDER,
  strikeCount: 10,
  selectedUnderlying: 'NIFTY',
  barDataSource: 'oi',
  barStyle: 'gradient',
}

// Bumped on every structural column change so users picking up the
// new build don't carry stale columnOrder from an older version that
// would misplace the new columns. Each bump abandons the previous
// stored prefs cleanly.
//   v1 → v2: Greeks moved from middle to outer edges
//   v2 → v3: OI Trend + Bias columns added per side
export const LOCALSTORAGE_KEY = 'openalgo_option_chain_prefs_v3'

// ---------------------------------------------------------------------
// OI Trend classification — used by the OptionChain table to derive
// "Long Buildup / Short Buildup / Long Unwinding / Short Covering" and
// a bullish/bearish bias from price + OI direction.
//
// Rules (per option leg — same convention as Zerodha Sensibull, Opstra):
//   Price ↑  + OI ↑  → Long Buildup     → Bullish
//   Price ↑  + OI ↓  → Short Covering    → Bullish
//   Price ↓  + OI ↑  → Short Buildup     → Bearish
//   Price ↓  + OI ↓  → Long Unwinding    → Bearish
//
// Returns null/'-' fields when ltp / prev_close / oi / prev_oi are
// missing or zero (no meaningful direction to compare).
// ---------------------------------------------------------------------

export type OiTrendLabel =
  | 'Long Buildup'
  | 'Short Buildup'
  | 'Long Unwinding'
  | 'Short Covering'
  | '-'

export type Bias = 'Bullish' | 'Bearish' | null

export function classifyOiTrend(
  ltp: number | null | undefined,
  prevClose: number | null | undefined,
  oi: number | null | undefined,
  prevOi: number | null | undefined
): { trend: OiTrendLabel; bias: Bias } {
  if (
    ltp == null ||
    prevClose == null ||
    oi == null ||
    prevOi == null ||
    ltp <= 0 ||
    prevClose <= 0 ||
    prevOi <= 0
  ) {
    return { trend: '-', bias: null }
  }
  const priceUp = ltp > prevClose
  const priceDown = ltp < prevClose
  const oiUp = oi > prevOi
  const oiDown = oi < prevOi
  if (priceUp && oiUp) return { trend: 'Long Buildup', bias: 'Bullish' }
  if (priceUp && oiDown) return { trend: 'Short Covering', bias: 'Bullish' }
  if (priceDown && oiUp) return { trend: 'Short Buildup', bias: 'Bearish' }
  if (priceDown && oiDown) return { trend: 'Long Unwinding', bias: 'Bearish' }
  return { trend: '-', bias: null }
}

// ---------------------------------------------------------------------
// Auto-refresh interval options for the page-level dropdown. Values are
// milliseconds — passed straight to useOptionChainLive's
// oiRefreshInterval option. Labels match the user's UX request.
// ---------------------------------------------------------------------

export interface RefreshIntervalOption {
  label: string
  ms: number
}

export const REFRESH_INTERVAL_OPTIONS: RefreshIntervalOption[] = [
  { label: '30 sec', ms: 30_000 },
  { label: '1 min', ms: 60_000 },
  { label: '3 min', ms: 180_000 },
  { label: '5 min', ms: 300_000 },
  { label: '15 min', ms: 900_000 },
  { label: '30 min', ms: 1_800_000 },
  { label: '1 hour', ms: 3_600_000 },
]

export const DEFAULT_REFRESH_INTERVAL_MS = 60_000  // 1 min
