/* Pure helpers for the ad-admin dashboard — no DOM, so they run under `node --test` as well. */

export function ctr(impressions, clicks) {
  return impressions ? clicks / impressions : 0;
}

export function fmtInt(n) {
  return new Intl.NumberFormat(undefined).format(Math.round(Number(n) || 0));
}

export function fmtPct(fraction, digits = 1) {
  return `${(100 * (Number(fraction) || 0)).toFixed(digits)} %`;
}

/** "+12.5 %" / "−3.0 %" / "n/a" (no baseline). */
export function fmtChange(change) {
  if (change === null || change === undefined || Number.isNaN(Number(change))) return 'n/a';
  const pct = 100 * Number(change);
  const sign = pct > 0 ? '+' : pct < 0 ? '−' : '±';
  return `${sign}${Math.abs(pct).toFixed(1)} %`;
}

export const KIND_LABELS = {
  internal: 'Internal ads',
  sponsored: 'Sponsored content',
  affiliate: 'Affiliate links',
  adsense: 'Google AdSense',
  microsoft: 'Microsoft Advertising',
  network: 'Third-party network',
};

export function kindLabel(kind) {
  return KIND_LABELS[kind] ?? kind;
}

/** Display status of a campaign row from the analytics/campaign APIs. */
export function campaignStatus(c) {
  if (c.status !== 'active') return c.status;
  return c.live ? 'live' : 'inactive';
}

/** Region name in the UI language ("DE" → "Germany"); "other" and "ZZ" get words. */
export function regionName(code, locale) {
  if (code === 'other') return 'Other (small counts folded)';
  if (code === 'ZZ') return 'No region';
  try {
    return new Intl.DisplayNames(locale ? [locale] : undefined, { type: 'region' }).of(code) ?? code;
  } catch {
    return code;
  }
}

/**
 * Line-chart geometry for a by-day series. Returns SVG path strings for impressions (area +
 * line) and clicks (line, own scale) plus axis ticks — the view only stamps them into <svg>.
 */
export function chartModel(byDay, width = 640, height = 180, pad = { top: 10, right: 12, bottom: 22, left: 40 }) {
  const points = Array.isArray(byDay) ? byDay : [];
  const n = points.length;
  const innerW = Math.max(1, width - pad.left - pad.right);
  const innerH = Math.max(1, height - pad.top - pad.bottom);
  const maxImp = Math.max(1, ...points.map((p) => p.impressions || 0));
  const maxClk = Math.max(1, ...points.map((p) => p.clicks || 0));
  const x = (i) => pad.left + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const yImp = (v) => pad.top + innerH - (v / maxImp) * innerH;
  const yClk = (v) => pad.top + innerH - (v / maxClk) * innerH;
  const line = (fn, key) =>
    points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${fn(p[key] || 0).toFixed(1)}`).join(' ');
  const impLine = n ? line(yImp, 'impressions') : '';
  const area = n
    ? `${impLine} L${x(n - 1).toFixed(1)} ${(pad.top + innerH).toFixed(1)} L${x(0).toFixed(1)} ${(pad.top + innerH).toFixed(1)} Z`
    : '';
  const tickEvery = n > 14 ? Math.ceil(n / 7) : 1;
  const ticks = points
    .map((p, i) => ({ x: x(i), label: (p.key || '').slice(5) }))
    .filter((_, i) => i % tickEvery === 0 || i === n - 1);
  return {
    width,
    height,
    impressionsPath: impLine,
    impressionsArea: area,
    clicksPath: n ? line(yClk, 'clicks') : '',
    maxImpressions: maxImp,
    maxClicks: maxClk,
    ticks,
    baseline: pad.top + innerH,
    dots: points.map((p, i) => ({ x: x(i), y: yImp(p.impressions || 0), p })),
  };
}

/** Bars as percentages of the largest value (for CSS-width bar charts). */
export function bars(items, key = 'impressions') {
  const max = Math.max(1, ...items.map((i) => i[key] || 0));
  return items.map((i) => ({ ...i, pct: Math.round((100 * (i[key] || 0)) / max) }));
}

/** Overall compliance status from the checks: fail > warn > ok. */
export function overallStatus(checks) {
  if (checks.some((c) => c.status === 'fail')) return 'fail';
  if (checks.some((c) => c.status === 'warn')) return 'warn';
  return 'ok';
}

/** Parse a `key=value,key2=value2` settings blob into rows (for the source settings chips). */
export function settingsRows(settings) {
  return Object.entries(settings || {}).map(([k, v]) => ({ key: k, value: String(v) }));
}
