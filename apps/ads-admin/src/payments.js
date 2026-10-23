/* Pure helpers for the Payments section of the admin console — no DOM, so they run under
 * `node --test` as well: money formatting, the date-range presets, the revenue-trend chart
 * geometry, share bars / donut arcs, alert summaries and the transaction status labels. */

export const STATUSES = ['succeeded', 'failed', 'refunded', 'partially_refunded'];

export const STATUS_LABELS = {
  succeeded: 'Succeeded',
  failed: 'Failed',
  refunded: 'Refunded',
  partially_refunded: 'Partially refunded',
};

export function statusLabel(status) {
  return STATUS_LABELS[status] ?? status;
}

/** Badge class for a transaction status. */
export function statusClass(status) {
  return { succeeded: 'ok', failed: 'fail', refunded: 'warn', partially_refunded: 'partial' }[status] ?? '';
}

/** "$1,234.50" in the UI locale; falls back to "1234.50 USD" for unknown currencies. */
export function fmtMoney(amount, currency = 'USD') {
  const n = Number(amount) || 0;
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency}`;
  }
}

export function fmtCents(cents, currency = 'USD') {
  return fmtMoney((Number(cents) || 0) / 100, currency);
}

/** The date-range presets of the toolbar; "custom" keeps whatever since/until say. */
export const PRESETS = [
  ['7', 'Last 7 days'],
  ['30', 'Last 30 days'],
  ['90', 'Last 90 days'],
  ['365', 'Last 12 months'],
  ['custom', 'Custom range'],
];

function isoDate(d) {
  return d.toISOString().slice(0, 10);
}

/** since/until (inclusive ISO dates) for a preset, `today` injectable for tests. */
export function presetRange(preset, today = new Date()) {
  const days = Number(preset);
  if (!days) return null;
  const until = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const since = new Date(until.getTime() - (days - 1) * 86400000);
  return { since: isoDate(since), until: isoDate(until) };
}

/** Query string for the analytics / transactions endpoints from the filter state. */
export function filterQuery({ since, until, method, status, q, limit, offset } = {}) {
  const parts = [];
  if (since) parts.push(`since=${encodeURIComponent(since)}`);
  if (until) parts.push(`until=${encodeURIComponent(until)}`);
  if (method) parts.push(`method=${encodeURIComponent(method)}`);
  if (status) parts.push(`status=${encodeURIComponent(status)}`);
  if (q) parts.push(`q=${encodeURIComponent(q)}`);
  if (limit) parts.push(`limit=${limit}`);
  if (offset) parts.push(`offset=${offset}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

/**
 * Revenue-trend geometry for the by-day series: gross revenue (area + line), net revenue
 * (line) and refunds (line) on one money scale, plus x ticks and hover dots.
 */
export function moneyChartModel(byDay, width = 720, height = 200, pad = { top: 10, right: 12, bottom: 22, left: 56 }) {
  const points = Array.isArray(byDay) ? byDay : [];
  const n = points.length;
  const innerW = Math.max(1, width - pad.left - pad.right);
  const innerH = Math.max(1, height - pad.top - pad.bottom);
  const max = Math.max(1, ...points.map((p) => Math.max(p.revenue_cents || 0, p.refunded_cents || 0)));
  const x = (i) => pad.left + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const y = (v) => pad.top + innerH - (Math.max(0, v) / max) * innerH;
  const line = (key) => points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p[key] || 0).toFixed(1)}`).join(' ');
  const revenue = n ? line('revenue_cents') : '';
  const bottom = (pad.top + innerH).toFixed(1);
  const area = n ? `${revenue} L${x(n - 1).toFixed(1)} ${bottom} L${x(0).toFixed(1)} ${bottom} Z` : '';
  const tickEvery = n > 14 ? Math.ceil(n / 7) : 1;
  const ticks = points
    .map((p, i) => ({ x: x(i), label: (p.key || '').slice(5) }))
    .filter((_, i) => i % tickEvery === 0 || i === n - 1);
  return {
    width,
    height,
    maxCents: max,
    revenuePath: revenue,
    revenueArea: area,
    netPath: n ? line('net_cents') : '',
    refundsPath: n ? line('refunded_cents') : '',
    ticks,
    baseline: pad.top + innerH,
    left: pad.left,
    dots: points.map((p, i) => ({ x: x(i), y: y(p.revenue_cents || 0), p })),
  };
}

/** Rows with a `pct` of the largest value (CSS-width bars), for any numeric key. */
export function shareBars(items, key) {
  const max = Math.max(1, ...items.map((i) => Number(i[key]) || 0));
  return items.map((i) => ({ ...i, pct: Math.round((100 * (Number(i[key]) || 0)) / max) }));
}

/**
 * Donut segments for a status / method share chart: stroke-dasharray pieces on a circle of
 * circumference 100 (so the values are percentages) — nothing to compute in the view.
 */
export function donut(items, key = 'count') {
  const total = items.reduce((s, i) => s + (Number(i[key]) || 0), 0);
  let offset = 0;
  return items.map((i) => {
    const pct = total ? (100 * (Number(i[key]) || 0)) / total : 0;
    const seg = { ...i, pct, dash: `${pct.toFixed(2)} ${(100 - pct).toFixed(2)}`, offset: (25 - offset).toFixed(2) };
    offset += pct;
    return seg;
  });
}

/** fail > warn > ok, and how many are not ok — for the alerts strip headline. */
export function alertSummary(alerts) {
  const list = Array.isArray(alerts) ? alerts : [];
  const fails = list.filter((a) => a.level === 'fail').length;
  const warns = list.filter((a) => a.level === 'warn').length;
  const level = fails ? 'fail' : warns ? 'warn' : 'ok';
  const text =
    level === 'ok'
      ? `All ${list.length} KPI checks pass`
      : `${fails ? `${fails} alert${fails > 1 ? 's' : ''}` : ''}${fails && warns ? ' · ' : ''}${warns ? `${warns} warning${warns > 1 ? 's' : ''}` : ''}`;
  return { level, fails, warns, text };
}

/** "−12.5 % vs. previous 30 days" with the sign coloured by the view. */
export function trendText(change, days) {
  if (change === null || change === undefined || Number.isNaN(Number(change))) return `no baseline (previous ${days} days)`;
  const pct = 100 * Number(change);
  const sign = pct > 0 ? '+' : pct < 0 ? '−' : '±';
  return `${sign}${Math.abs(pct).toFixed(1)} % vs. previous ${days} days`;
}

export function trendClass(change, invert = false) {
  if (change === null || change === undefined || Number.isNaN(Number(change)) || Number(change) === 0) return '';
  const up = Number(change) > 0;
  return (invert ? !up : up) ? 'up' : 'down';
}
