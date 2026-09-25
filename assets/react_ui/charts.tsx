"use client"

/**
 * Chart components, on ECharts.
 *
 * The formatting rules here mirror the Streamlit app deliberately. The two surfaces read
 * the same reporting views, so if one renders $23.0M and the other 22964557.17 for the
 * same metric, a viewer comparing them reasonably concludes one is wrong. Same reason the
 * axis headroom and tail-collapsing behaviour are duplicated: a bar whose label is
 * clipped at the plot edge in one app and not the other looks like a data difference.
 *
 * On the vocabulary. This file carries eight shapes rather than the two it started with.
 * That is not decoration: the source Cognos Framework Manager model declares no reports
 * and no charts at all -- it is a modelling layer -- so there is no report-parity argument
 * for staying plain, and each shape below is picked for a question the ranked bar cannot
 * answer. Every one of them aggregates rows the page already holds; none issues a query.
 */

import ReactECharts from "echarts-for-react"

import { SF, SERIES, DIM, SCALE } from "@/lib/theme"

const MAX_CATEGORIES = 8

export type Row = Record<string, unknown>

const num = (v: unknown): number => {
  const n = typeof v === "number" ? v : parseFloat(String(v ?? ""))
  return Number.isFinite(n) ? n : 0
}

/** Compact currency. `$0` rather than `$0.0`, and `--` for nothing at all. */
export function usd(v: unknown): string {
  const n = num(v)
  const a = Math.abs(n)
  const sign = n < 0 ? "-" : ""
  if (a === 0) return "$0"
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(1)}B`
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`
  if (a >= 1e3) return `${sign}$${Math.round(a / 1e3)}K`
  return `${sign}$${Math.round(a).toLocaleString()}`
}

export function pct(v: unknown): string {
  let n = num(v)
  // Source metrics express growth as a fraction; anything past +/-5 is already in
  // percentage points and would otherwise render as 450%.
  if (Math.abs(n) <= 5) n *= 100
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`
}

/**
 * A proportion of a whole, unsigned.
 *
 * Separate from `pct` because `pct` is deliberately signed -- it exists for growth
 * deltas, where the sign is the point. Using it for a share rendered
 * "+33.2% of sales arrives with no customer attribution", which reads as an increase
 * rather than as a third of the total. That was written twice, once per surface,
 * because neither library offered the unsigned form; now both do, under the same name.
 *
 * Accepts a fraction (0.332) or already-scaled percentage points (33.2), on the same
 * rule `pct` uses.
 */
export function share(v: unknown): string {
  let n = num(v)
  if (Math.abs(n) <= 5) n *= 100
  return `${n.toFixed(1)}%`
}

export function count(v: unknown): string {
  return Math.round(num(v)).toLocaleString()
}

/**
 * Keep the top N-1 categories and fold the remainder into "Other".
 *
 * The total is preserved on purpose. Truncating to a top-N and dropping the tail makes
 * the bars disagree with the KPI above them, which is a worse failure than a crowded
 * chart because it is silent.
 */
function collapseTail(rows: { name: string; value: number }[]) {
  const ranked = [...rows].sort((a, b) => b.value - a.value)
  if (ranked.length <= MAX_CATEGORIES) return ranked
  const head = ranked.slice(0, MAX_CATEGORIES - 1)
  const tail = ranked.slice(MAX_CATEGORIES - 1)
  return [
    ...head,
    { name: `Other (${tail.length})`, value: tail.reduce((s, r) => s + r.value, 0) },
  ]
}

const BASE = {
  backgroundColor: "transparent",
  grid: { left: 8, right: 24, top: 12, bottom: 8, containLabel: true },
  textStyle: { fontFamily: "system-ui, sans-serif", fontSize: 12 },
}

/** Group rows to a single numeric measure per key, in insertion order. */
function rollup(rows: Row[], labelKey: string, valueKey: string) {
  const acc = new Map<string, number>()
  for (const r of rows) {
    const k = String(r[labelKey] ?? "")
    if (!k || k === "null" || k === "undefined") continue
    acc.set(k, (acc.get(k) ?? 0) + num(r[valueKey]))
  }
  return acc
}

/**
 * Inline SVG sparkline.
 *
 * SVG rather than an ECharts instance per KPI tile: four more chart instances on first
 * paint to draw four trend lines 104px wide is a poor trade, and the Streamlit app draws
 * its sparklines the same way so the two look identical.
 */
export function Sparkline({ values, color = SF.blue }: {
  values: number[]
  color?: string
}) {
  const pts = values.filter(v => Number.isFinite(v))
  if (pts.length < 2) return null
  const lo = Math.min(...pts)
  const hi = Math.max(...pts)
  const span = hi - lo || 1
  const w = 104, h = 24, step = w / (pts.length - 1)
  const coords = pts
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - lo) / span) * (h - 4) - 2).toFixed(1)}`)
    .join(" ")
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
         className="mt-1 block">
      <polyline points={coords} fill="none" stroke={color} strokeWidth={1.6}
                strokeLinejoin="round" />
    </svg>
  )
}

export function RankedBar({
  rows, labelKey, valueKey, format = usd, selected, onSelect,
}: {
  rows: Row[]
  labelKey: string
  valueKey: string
  format?: (v: unknown) => string
  selected?: string | null
  onSelect?: (label: string | null) => void
}) {
  // Aggregate in the browser: these datasets are small and the alternative is a query
  // per interaction, which makes a click feel slow for no benefit.
  const grouped = new Map<string, number>()
  for (const r of rows) {
    const k = String(r[labelKey] ?? "")
    if (!k) continue
    grouped.set(k, (grouped.get(k) ?? 0) + num(r[valueKey]))
  }
  const data = collapseTail([...grouped].map(([name, value]) => ({ name, value })))
                 .reverse()   // ECharts draws a horizontal category axis bottom-up
  const peak = Math.max(...data.map(d => d.value), 0)

  const option = {
    ...BASE,
    // 18% headroom so an outside value label cannot be clipped by the plot edge.
    xAxis: { type: "value", max: peak ? peak * 1.18 : undefined, show: false },
    yAxis: {
      type: "category",
      data: data.map(d => d.name),
      axisTick: { show: false },
      axisLine: { show: false },
    },
    tooltip: {
      trigger: "item",
      formatter: (p: { name: string; value: number }) =>
        `${p.name}: ${format(p.value)}`,
    },
    series: [{
      type: "bar",
      data: data.map(d => ({
        value: d.value,
        itemStyle: {
          color: !selected || d.name === selected ? SERIES[0] : DIM,
          borderRadius: [0, 3, 3, 0],
        },
      })),
      label: {
        show: true, position: "right",
        formatter: (p: { value: number }) => format(p.value),
        fontSize: 11,
      },
      barMaxWidth: 22,
    }],
  }

  // Floor is 140, not 220: a one-bar chart in a 220px panel reads as a chart that failed
  // to draw the rest of its data. The US alone accounts for most of the customer book, so
  // "sales by country" is legitimately a very short chart.
  const height = Math.max(140, data.length * 34 + 40)

  return (
    <ReactECharts
      option={option}
      style={{ height }}
      onEvents={onSelect ? {
        // Clicking the selected bar clears the filter, which is the behaviour people
        // expect and saves a trip to the reset button.
        click: (p: { name: string }) => onSelect(p.name === selected ? null : p.name),
      } : undefined}
      notMerge
    />
  )
}

export function TrendLines({
  rows, xKey, series, sortKey,
}: {
  rows: Row[]
  xKey: string
  series: { key: string; label: string }[]
  sortKey?: string
}) {
  const sorted = sortKey
    ? [...rows].sort((a, b) => num(a[sortKey]) - num(b[sortKey]))
    : rows

  // Collapse to one point per x value, or repeated fiscal months draw a sawtooth.
  const order: string[] = []
  const acc = new Map<string, Record<string, number>>()
  for (const r of sorted) {
    const k = String(r[xKey] ?? "")
    if (!k) continue
    if (!acc.has(k)) { acc.set(k, {}); order.push(k) }
    const bucket = acc.get(k)!
    for (const s of series) bucket[s.key] = (bucket[s.key] ?? 0) + num(r[s.key])
  }

  const option = {
    ...BASE,
    grid: { ...BASE.grid, bottom: 48, top: 34 },
    legend: { data: series.map(s => s.label), top: 0, left: 0, itemHeight: 8 },
    tooltip: { trigger: "axis", valueFormatter: (v: number) => usd(v) },
    xAxis: {
      type: "category",
      data: order,
      axisLabel: { rotate: 45, fontSize: 10 },
    },
    yAxis: { type: "value", axisLabel: { formatter: (v: number) => usd(v) } },
    series: series.map((s, i) => ({
      name: s.label,
      type: "line",
      smooth: false,
      symbolSize: 5,
      lineStyle: { width: 2.2, color: SERIES[i % SERIES.length] },
      itemStyle: { color: SERIES[i % SERIES.length] },
      data: order.map(k => acc.get(k)![s.key] ?? 0),
    })),
  }

  return <ReactECharts option={option} style={{ height: 340 }} notMerge />
}

/**
 * Two series with the space between them filled.
 *
 * Implemented as two *stacked* areas rather than as a fill-to-next-series, because the
 * band is the point: the lower area is sales, the upper area is bookings minus sales, and
 * the top edge of the stack is therefore bookings. The filled band literally *is* backlog,
 * so the chart explains the metric rather than needing a footnote under it.
 *
 * The consequence is that the second series holds a difference, not a total, so the
 * tooltip is formatted explicitly -- left to itself it would report the delta under the
 * word "Bookings", which would be wrong.
 */
export function AreaGap({
  rows, xKey, sortKey, lowerKey, upperKey, lowerLabel, upperLabel, gapLabel,
}: {
  rows: Row[]
  xKey: string
  sortKey?: string
  lowerKey: string
  upperKey: string
  lowerLabel: string
  upperLabel: string
  gapLabel: string
}) {
  const sorted = sortKey
    ? [...rows].sort((a, b) => num(a[sortKey]) - num(b[sortKey]))
    : rows

  const order: string[] = []
  const acc = new Map<string, { lower: number; upper: number }>()
  for (const r of sorted) {
    const k = String(r[xKey] ?? "")
    if (!k) continue
    if (!acc.has(k)) { acc.set(k, { lower: 0, upper: 0 }); order.push(k) }
    const b = acc.get(k)!
    b.lower += num(r[lowerKey])
    b.upper += num(r[upperKey])
  }

  const lower = order.map(k => acc.get(k)!.lower)
  const upper = order.map(k => acc.get(k)!.upper)
  // Clamped at zero: a month where shipments exceed orders would otherwise stack a
  // negative band downward and make the top edge stop being bookings.
  const gap = order.map((_, i) => Math.max(0, upper[i] - lower[i]))

  const option = {
    ...BASE,
    grid: { ...BASE.grid, bottom: 48, top: 34 },
    legend: { data: [lowerLabel, gapLabel], top: 0, left: 0, itemHeight: 8 },
    tooltip: {
      trigger: "axis",
      formatter: (ps: { dataIndex: number; axisValue: string }[]) => {
        const i = ps[0]?.dataIndex ?? 0
        return [
          `<strong>${ps[0]?.axisValue ?? ""}</strong>`,
          `${upperLabel}: ${usd(upper[i])}`,
          `${lowerLabel}: ${usd(lower[i])}`,
          `${gapLabel}: ${usd(gap[i])}`,
        ].join("<br/>")
      },
    },
    xAxis: { type: "category", data: order, axisLabel: { rotate: 45, fontSize: 10 } },
    yAxis: { type: "value", axisLabel: { formatter: (v: number) => usd(v) } },
    series: [
      {
        name: lowerLabel, type: "line", stack: "band", symbol: "none",
        lineStyle: { width: 2.2, color: SF.star },
        itemStyle: { color: SF.star },
        areaStyle: { color: "rgba(17,86,127,.20)" },
        data: lower,
      },
      {
        name: gapLabel, type: "line", stack: "band", symbol: "none",
        lineStyle: { width: 2.2, color: SF.blue },
        itemStyle: { color: SF.accent },
        areaStyle: { color: "rgba(255,159,54,.30)" },
        data: gap,
      },
    ],
  }

  return <ReactECharts option={option} style={{ height: 340 }} notMerge />
}

/**
 * Year-over-year movement, decomposed by category.
 *
 * ECharts has no waterfall type, so this is the standard construction: an invisible
 * "placeholder" bar that lifts each visible bar to its starting height, stacked with the
 * movement bar itself.
 *
 * The comparison years are the two most recent COMPLETE fiscal years, chosen by counting
 * distinct fiscal months in the data. That is not fussiness -- the first build of this
 * chart compared FY2026 against a three-month-old FY2027 and drew a 75M cliff that was
 * elapsed time rather than lost business. A chart should not be less careful than the
 * agent, which volunteers that a partial year is not comparable when asked.
 */
export function Waterfall({
  rows, yearKey, monthKey, categoryKey, valueKey,
}: {
  rows: Row[]
  yearKey: string
  monthKey?: string
  categoryKey: string
  valueKey: string
}) {
  const years = [...new Set(rows.map(r => String(r[yearKey] ?? "")).filter(Boolean))].sort()
  if (years.length < 2) {
    return (
      <p className="py-8 text-sm text-slate-500">
        Two fiscal years are needed to show a movement. The current filters leave fewer.
      </p>
    )
  }

  // Distinct fiscal months per year, to tell a complete year from a partial one.
  const monthsPer = new Map<string, Set<string>>()
  if (monthKey) {
    for (const r of rows) {
      const y = String(r[yearKey] ?? "")
      if (!y) continue
      if (!monthsPer.has(y)) monthsPer.set(y, new Set())
      monthsPer.get(y)!.add(String(r[monthKey] ?? ""))
    }
  }
  const full = monthsPer.size
    ? Math.max(...[...monthsPer.values()].map(s => s.size))
    : 0
  const complete = full ? years.filter(y => (monthsPer.get(y)?.size ?? 0) >= full) : years
  const [prior, latest] = complete.length >= 2
    ? [complete[complete.length - 2], complete[complete.length - 1]]
    : [years[years.length - 2], years[years.length - 1]]
  const excluded = years.filter(y => y > latest)

  const priorBy = rollup(rows.filter(r => String(r[yearKey]) === prior), categoryKey, valueKey)
  const latestBy = rollup(rows.filter(r => String(r[yearKey]) === latest), categoryKey, valueKey)
  const cats = [...new Set([...priorBy.keys(), ...latestBy.keys()])]

  let deltas = cats
    .map(c => ({ name: c, value: (latestBy.get(c) ?? 0) - (priorBy.get(c) ?? 0) }))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))

  // Fold the tail into one bar rather than dropping it. An incomplete waterfall does not
  // reconcile to its own end bar, which is the one thing a waterfall is for.
  if (deltas.length > MAX_CATEGORIES - 2) {
    const head = deltas.slice(0, MAX_CATEGORIES - 3)
    const tail = deltas.slice(MAX_CATEGORIES - 3)
    deltas = [...head, {
      name: `Other (${tail.length})`,
      value: tail.reduce((s, d) => s + d.value, 0),
    }]
  }

  const startTotal = [...priorBy.values()].reduce((s, v) => s + v, 0)
  const endTotal = [...latestBy.values()].reduce((s, v) => s + v, 0)

  const labels = [prior, ...deltas.map(d => d.name), latest]
  const placeholder: (number | string)[] = [0]
  const rises: (number | string)[] = ["-"]
  const falls: (number | string)[] = ["-"]
  const totals: (number | string)[] = [startTotal]

  let running = startTotal
  for (const d of deltas) {
    if (d.value >= 0) {
      placeholder.push(running)
      rises.push(d.value)
      falls.push("-")
    } else {
      placeholder.push(running + d.value)
      rises.push("-")
      falls.push(-d.value)
    }
    running += d.value
    totals.push("-")
  }
  placeholder.push(0)
  rises.push("-")
  falls.push("-")
  totals.push(endTotal)

  const movement = startTotal ? ((endTotal - startTotal) / startTotal) * 100 : 0
  const top = Math.max(startTotal, endTotal, running)

  const option = {
    ...BASE,
    grid: { ...BASE.grid, bottom: 56, top: 20 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (ps: { dataIndex: number; axisValue: string }[]) => {
        const i = ps[0]?.dataIndex ?? 0
        if (i === 0) return `<strong>${prior}</strong><br/>Total ${usd(startTotal)}`
        if (i === labels.length - 1) return `<strong>${latest}</strong><br/>Total ${usd(endTotal)}`
        const d = deltas[i - 1]
        return `<strong>${d.name}</strong><br/>${d.value >= 0 ? "+" : "-"}${usd(Math.abs(d.value))}`
      },
    },
    xAxis: { type: "category", data: labels, axisLabel: { rotate: 35, fontSize: 10 } },
    yAxis: {
      type: "value", max: top ? top * 1.2 : undefined,
      axisLabel: { formatter: (v: number) => usd(v) },
    },
    series: [
      // The lifter. `stack` plus a transparent style is what makes the visible bars
      // float; ECharts has no first-class waterfall.
      {
        name: "placeholder", type: "bar", stack: "wf", silent: true,
        itemStyle: { color: "transparent" },
        emphasis: { itemStyle: { color: "transparent" } },
        data: placeholder,
      },
      {
        name: "Grew", type: "bar", stack: "wf", barMaxWidth: 46,
        itemStyle: { color: SF.blue }, data: rises,
        label: { show: true, position: "top", fontSize: 10,
                 formatter: (p: { value: number | string }) =>
                   typeof p.value === "number" ? `+${usd(p.value)}` : "" },
      },
      {
        name: "Shrank", type: "bar", stack: "wf", barMaxWidth: 46,
        itemStyle: { color: SF.accent }, data: falls,
        label: { show: true, position: "bottom", fontSize: 10,
                 formatter: (p: { value: number | string }) =>
                   typeof p.value === "number" ? `-${usd(p.value)}` : "" },
      },
      {
        name: "Year total", type: "bar", stack: "wf", barMaxWidth: 46,
        itemStyle: { color: SF.star }, data: totals,
        label: { show: true, position: "top", fontSize: 11, fontWeight: "bold",
                 formatter: (p: { value: number | string }) =>
                   typeof p.value === "number" ? usd(p.value) : "" },
      },
    ],
  }

  return (
    <>
      <ReactECharts option={option} style={{ height: 380 }} notMerge />
      <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
        {prior} to {latest}: {movement >= 0 ? "+" : ""}{movement.toFixed(1)}%. Blue grew,
        orange shrank, and the two dark bars are the year totals they reconcile to.
        {excluded.length > 0 && (
          <> {excluded.join(", ")} is excluded as a partial year &mdash; on a waterfall it
          would draw as a collapse rather than as elapsed time.</>
        )}
      </p>
    </>
  )
}

/**
 * Two categorical axes against one measure.
 *
 * Surfaces concentration no bar chart shows, because a bar chart has to pick one of the
 * two axes and sum the other away.
 */
export function Heatmap({
  rows, rowKey, colKey, valueKey, colOrder,
}: {
  rows: Row[]
  rowKey: string
  colKey: string
  valueKey: string
  colOrder?: string[]
}) {
  const cells = new Map<string, number>()
  const rowTotals = new Map<string, number>()
  const colSeen: string[] = []
  for (const r of rows) {
    const y = String(r[rowKey] ?? "")
    const x = String(r[colKey] ?? "")
    if (!y || !x) continue
    const v = num(r[valueKey])
    cells.set(`${y}\u0000${x}`, (cells.get(`${y}\u0000${x}`) ?? 0) + v)
    rowTotals.set(y, (rowTotals.get(y) ?? 0) + v)
    if (!colSeen.includes(x)) colSeen.push(x)
  }

  const cols = colOrder ? colOrder.filter(c => colSeen.includes(c)) : colSeen
  // Rows ordered by total so the densest band sits at the top rather than wherever the
  // alphabet happens to put it. Ascending, because ECharts draws a category axis bottom-up.
  const rowNames = [...rowTotals.entries()].sort((a, b) => a[1] - b[1]).map(([k]) => k)
  const data: [number, number, number][] = []
  let peak = 0
  rowNames.forEach((y, yi) => cols.forEach((x, xi) => {
    const v = cells.get(`${y}\u0000${x}`)
    if (v == null) return
    peak = Math.max(peak, v)
    data.push([xi, yi, v])
  }))

  const option = {
    ...BASE,
    grid: { ...BASE.grid, bottom: 56, top: 12, right: 70 },
    tooltip: {
      position: "top",
      formatter: (p: { value: [number, number, number] }) =>
        `${rowNames[p.value[1]]} / ${cols[p.value[0]]}<br/><strong>${usd(p.value[2])}</strong>`,
    },
    xAxis: {
      type: "category", data: cols, splitArea: { show: true },
      axisLabel: { rotate: 45, fontSize: 10 },
    },
    yAxis: { type: "category", data: rowNames, splitArea: { show: true } },
    visualMap: {
      min: 0, max: peak || 1, calculable: false, orient: "vertical",
      right: 0, top: "middle", itemHeight: 120, itemWidth: 11,
      inRange: { color: [...SCALE] },
      textStyle: { fontSize: 10 },
      // Endpoint labels set explicitly. A continuous visualMap renders `formatter` against
      // its `text` pair, and with no `text` it draws a bare gradient with no numbers on it
      // -- which is a legend that explains nothing.
      text: [usd(peak), "$0"],
    },
    series: [{
      type: "heatmap", data,
      itemStyle: { borderWidth: 1, borderColor: "rgba(255,255,255,.7)" },
      emphasis: { itemStyle: { borderColor: SF.star, borderWidth: 2 } },
    }],
  }

  return (
    <ReactECharts option={option}
                  style={{ height: Math.max(260, rowNames.length * 42 + 110) }} notMerge />
  )
}

/**
 * Build a nested tree for treemap and sunburst.
 *
 * Nodes are keyed by their full path rather than by label. A label legitimately repeats
 * under two parents -- the same product group sells in two regions -- and keying by bare
 * label merges those into one node, which quietly doubles a branch.
 */
type TreeNode = { name: string; value: number; children?: TreeNode[]; itemStyle?: object }

function buildTree(rows: Row[], levels: string[], valueKey: string): TreeNode[] {
  type Interim = { name: string; value: number; kids: Map<string, Interim> }
  const roots = new Map<string, Interim>()

  for (const r of rows) {
    const path = levels.map(l => String(r[l] ?? "").trim()).filter(Boolean)
    if (!path.length) continue
    const v = num(r[valueKey])
    if (!v) continue
    let level = roots
    for (const seg of path) {
      if (!level.has(seg)) level.set(seg, { name: seg, value: 0, kids: new Map() })
      const node = level.get(seg)!
      node.value += v
      level = node.kids
    }
  }

  const toNodes = (m: Map<string, Interim>, depth: number): TreeNode[] =>
    [...m.values()]
      .sort((a, b) => b.value - a.value)
      .map((n, i) => ({
        name: n.name,
        value: n.value,
        ...(depth === 0 ? { itemStyle: { color: SERIES[i % SERIES.length] } } : {}),
        ...(n.kids.size ? { children: toNodes(n.kids, depth + 1) } : {}),
      }))

  return toNodes(roots, 0)
}

/** Two hierarchy levels at once, sized by measure. Where the business is, at a glance,
 *  without making anyone read an axis. */
export function Treemap({ rows, levels, valueKey }: {
  rows: Row[]; levels: string[]; valueKey: string
}) {
  const data = buildTree(rows, levels, valueKey)
  if (!data.length) {
    return <p className="py-8 text-sm text-slate-500">No hierarchy rows match the current filters.</p>
  }
  const option = {
    ...BASE,
    tooltip: {
      formatter: (p: { treePathInfo?: { name: string }[]; value: number }) =>
        `${(p.treePathInfo ?? []).slice(1).map(t => t.name).join(" / ")}<br/>` +
        `<strong>${usd(p.value)}</strong>`,
    },
    series: [{
      type: "treemap", data, roam: false,
      // Breadcrumb off: this is a two-level treemap on a dashboard, not an explorer, and
      // the breadcrumb bar eats vertical space that the tiles need.
      breadcrumb: { show: false },
      nodeClick: false,
      levels: [
        { itemStyle: { borderWidth: 3, borderColor: "#fff", gapWidth: 3 } },
        { itemStyle: { borderWidth: 1, borderColor: "rgba(255,255,255,.75)", gapWidth: 1 },
          colorSaturation: [0.35, 0.7] },
      ],
      label: {
        show: true, fontSize: 11,
        formatter: (p: { name: string; value: number }) => `${p.name}\n${usd(p.value)}`,
      },
      upperLabel: { show: true, height: 20, fontSize: 11, fontWeight: "bold" },
    }],
  }
  return <ReactECharts option={option} style={{ height: 430 }} notMerge />
}

/** The drill structure itself, as a shape. Uses the deep hierarchy the source model
 *  declares rather than a flattened two levels. */
export function Sunburst({ rows, levels, valueKey }: {
  rows: Row[]; levels: string[]; valueKey: string
}) {
  const data = buildTree(rows, levels, valueKey)
  if (!data.length) {
    return <p className="py-8 text-sm text-slate-500">No hierarchy rows match the current filters.</p>
  }
  const option = {
    ...BASE,
    tooltip: {
      formatter: (p: { treePathInfo?: { name: string }[]; value: number }) =>
        `${(p.treePathInfo ?? []).slice(1).map(t => t.name).join(" / ")}<br/>` +
        `<strong>${usd(p.value)}</strong>`,
    },
    series: [{
      type: "sunburst", data, radius: [0, "92%"],
      emphasis: { focus: "ancestor" },
      label: { minAngle: 8, fontSize: 10, rotate: "radial" },
      levels: [
        {},
        { r0: "0%", r: "34%", itemStyle: { borderWidth: 2, borderColor: "#fff" },
          label: { rotate: "tangential", fontSize: 11 } },
        { r0: "34%", r: "62%", itemStyle: { borderWidth: 1.5, borderColor: "#fff" } },
        { r0: "62%", r: "82%", itemStyle: { borderWidth: 1, borderColor: "#fff" } },
        { r0: "82%", r: "92%", itemStyle: { borderWidth: 1, borderColor: "#fff" },
          label: { show: false } },
      ],
    }],
  }
  return (
    <>
      <ReactECharts option={option} style={{ height: 430 }} notMerge />
      <p className="mt-2 text-[11px] text-slate-500">
        Click a segment to zoom a branch, click the centre to come back out. The levels are
        the ones the source model declares, not levels we invented.
      </p>
    </>
  )
}

/** Share of total by period. Normalised deliberately: absolute stacked bars answer "did we
 *  grow", which the trend already answers, and hide whether the mix is moving. */
export function MixBar({ rows, xKey, seriesKey, valueKey }: {
  rows: Row[]; xKey: string; seriesKey: string; valueKey: string
}) {
  const cells = new Map<string, number>()
  const xTotals = new Map<string, number>()
  const catTotals = new Map<string, number>()
  for (const r of rows) {
    const x = String(r[xKey] ?? "")
    const c = String(r[seriesKey] ?? "")
    if (!x || !c) continue
    const v = num(r[valueKey])
    cells.set(`${x}\u0000${c}`, (cells.get(`${x}\u0000${c}`) ?? 0) + v)
    xTotals.set(x, (xTotals.get(x) ?? 0) + v)
    catTotals.set(c, (catTotals.get(c) ?? 0) + v)
  }

  const xs = [...xTotals.keys()].sort()
  let cats = [...catTotals.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k)
  if (!xs.length) {
    return <p className="py-8 text-sm text-slate-500">No data matches the current filters.</p>
  }

  /**
   * Fold the tail into "Other", same MAX_CATEGORIES rule the ranked bar uses.
   *
   * Not cosmetic. The regional split has thirteen values, and thirteen legend entries wrap
   * to three rows, overlap the 100% axis label and cover the top of the bars -- observed,
   * not theorised. Folding also keeps every period summing to exactly 100%, because the
   * tail is aggregated rather than dropped.
   */
  if (cats.length > MAX_CATEGORIES) {
    const keep = cats.slice(0, MAX_CATEGORIES - 1)
    const tail = cats.slice(MAX_CATEGORIES - 1)
    const otherName = `Other (${tail.length})`
    for (const x of xs) {
      const merged = tail.reduce(
        (s, c) => s + (cells.get(`${x}\u0000${c}`) ?? 0), 0)
      if (merged) cells.set(`${x}\u0000${otherName}`, merged)
    }
    cats = [...keep, otherName]
  }

  const option = {
    ...BASE,
    grid: { ...BASE.grid, bottom: 12, top: 52 },
    legend: { data: cats, top: 0, left: 0, itemHeight: 8, textStyle: { fontSize: 11 } },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v: number) => `${num(v).toFixed(1)}%`,
    },
    xAxis: { type: "category", data: xs },
    yAxis: { type: "value", max: 100, axisLabel: { formatter: "{value}%" } },
    series: cats.map((c, i) => ({
      name: c, type: "bar", stack: "mix", barMaxWidth: 58,
      itemStyle: { color: SERIES[i % SERIES.length] },
      data: xs.map(x => {
        const total = xTotals.get(x) ?? 0
        // Guarded rather than assumed: a period whose measures net to zero would
        // otherwise produce Infinity and make every other bar invisible.
        return total ? ((cells.get(`${x}\u0000${c}`) ?? 0) / total) * 100 : 0
      }),
    })),
  }
  return <ReactECharts option={option} style={{ height: 360 }} notMerge />
}

/**
 * Ranked bars with a cumulative share curve.
 *
 * The 80% line is the point: concentration is a risk question, and "104 customers are 80%
 * of the book" lands in a way a ranked list does not. The curve is computed over every
 * row, not just the bars drawn, so the percentage is true rather than
 * true-of-the-top-twenty.
 */
export function Pareto({ rows, labelKey, valueKey, topN = 20 }: {
  rows: Row[]; labelKey: string; valueKey: string; topN?: number
}) {
  const ranked = [...rollup(rows, labelKey, valueKey)]
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
  const grand = ranked.reduce((s, r) => s + r.value, 0)
  if (grand <= 0) {
    return <p className="py-8 text-sm text-slate-500">Nothing to rank under the current filters.</p>
  }

  let running = 0
  const cum = ranked.map(r => { running += r.value; return (running / grand) * 100 })
  const n80 = (cum.findIndex(c => c >= 80) + 1) || ranked.length

  const shown = ranked.slice(0, topN)
  const peak = Math.max(...shown.map(r => r.value), 0)

  /**
   * The right-hand axis is scaled to the curve actually drawn, not fixed at 100%.
   *
   * This data is close to uniform -- 104 of 266 customers make up 80% -- so the top twenty
   * only reach about 20% cumulative. Against a 0-100% axis that curve is a flat line along
   * the floor with no readable shape, and the 80% reference line sits in empty space above
   * it pretending to be informative. Scaling to the curve gives it shape, and the 80% line
   * is drawn only when it is actually in range. The concentration figure is stated in the
   * caption either way, which is where it belongs: it is a fact about all 266 customers,
   * not about the twenty bars.
   */
  const maxCum = Math.max(...shown.map((_, i) => cum[i]), 0)
  // Round up to the next 5% above the curve plus a small margin, so the top tick sits just
  // above the last point rather than a whole decade above it.
  const rightMax = maxCum >= 70 ? 105 : Math.min(105, Math.ceil((maxCum * 1.15) / 5) * 5)
  const showEightyLine = rightMax >= 80

  const option = {
    ...BASE,
    grid: { ...BASE.grid, bottom: 96, top: 34, right: 52 },
    legend: { data: ["Sales", "Cumulative share"], top: 0, left: 0, itemHeight: 8 },
    tooltip: {
      trigger: "axis",
      formatter: (ps: { dataIndex: number; axisValue: string }[]) => {
        const i = ps[0]?.dataIndex ?? 0
        return `<strong>${shown[i]?.name ?? ""}</strong><br/>` +
               `Sales: ${usd(shown[i]?.value)}<br/>` +
               `Cumulative: ${cum[i]?.toFixed(1)}% of total`
      },
    },
    xAxis: {
      type: "category", data: shown.map(r => r.name),
      axisLabel: { rotate: 45, fontSize: 9, width: 90, overflow: "truncate" },
    },
    yAxis: [
      { type: "value", max: peak ? peak * 1.12 : undefined,
        axisLabel: { formatter: (v: number) => usd(v) } },
      { type: "value", max: rightMax, splitLine: { show: false },
        axisLabel: { formatter: "{value}%" } },
    ],
    series: [
      {
        name: "Sales", type: "bar", barMaxWidth: 30,
        itemStyle: { color: SF.blue, borderRadius: [3, 3, 0, 0] },
        data: shown.map(r => r.value),
      },
      {
        name: "Cumulative share", type: "line", yAxisIndex: 1, symbolSize: 5,
        lineStyle: { width: 2.4, color: SF.accent },
        itemStyle: { color: SF.accent },
        data: shown.map((_, i) => cum[i]),
        ...(showEightyLine ? {
          markLine: {
            silent: true, symbol: "none",
            data: [{ yAxis: 80,
                     lineStyle: { color: SF.purple, type: "dashed", width: 1.2 } }],
            // Label on the left, away from the right-hand axis ticks it was colliding
            // with at 80% and 100%.
            label: { formatter: "80%", position: "insideStartTop", fontSize: 10,
                     color: SF.purple },
          },
        } : {}),
      },
    ],
  }

  return (
    <>
      <ReactECharts option={option} style={{ height: 380 }} notMerge />
      <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
        {n80.toLocaleString()} of {ranked.length.toLocaleString()} account for 80% of sales
        in this scope &mdash; revenue here is <strong>not</strong> concentrated in a handful
        of accounts. The curve is cumulative over all {ranked.length.toLocaleString()},
        so the top {shown.length} shown reach {maxCum.toFixed(1)}%.
      </p>
    </>
  )
}

/** Two bars, one point. Used only by the fiscal-calendar panel, where the whole content is
 *  a single comparison and a full chart abstraction would be more code than the chart. */
export function CompareBars({ items }: {
  items: { label: string; value: number; color: string }[]
}) {
  const peak = Math.max(...items.map(i => i.value), 0)
  const option = {
    ...BASE,
    grid: { ...BASE.grid, bottom: 12, top: 28 },
    tooltip: {
      trigger: "item",
      formatter: (p: { name: string; value: number }) =>
        `${p.name}<br/><strong>${usd(p.value)}</strong>`,
    },
    xAxis: { type: "category", data: items.map(i => i.label),
             axisLabel: { fontSize: 11, interval: 0 } },
    yAxis: { type: "value", max: peak ? peak * 1.18 : undefined,
             axisLabel: { formatter: (v: number) => usd(v) } },
    series: [{
      type: "bar", barMaxWidth: 96,
      data: items.map(i => ({
        value: i.value, name: i.label,
        itemStyle: { color: i.color, borderRadius: [4, 4, 0, 0] },
      })),
      label: { show: true, position: "top", fontSize: 13, fontWeight: "bold",
               formatter: (p: { value: number }) => usd(p.value) },
    }],
  }
  return <ReactECharts option={option} style={{ height: 320 }} notMerge />
}
