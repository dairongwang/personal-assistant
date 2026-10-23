// Run: node --test apps/ads-admin/tests   (no dependencies)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  alertSummary,
  donut,
  filterQuery,
  fmtCents,
  fmtMoney,
  moneyChartModel,
  presetRange,
  shareBars,
  statusClass,
  statusLabel,
  trendClass,
  trendText,
} from '../src/payments.js';

test('money formatting', () => {
  assert.equal(fmtMoney('1234.5', 'USD'), new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(1234.5));
  assert.equal(fmtCents(1000, 'USD'), fmtMoney('10.00', 'USD'));
  assert.equal(fmtMoney('abc', 'USD'), fmtMoney('0', 'USD'));
  assert.equal(fmtMoney('2.5', 'NOPE'), '2.50 NOPE'); // unknown currency: plain fallback
});

test('status labels and classes', () => {
  assert.equal(statusLabel('partially_refunded'), 'Partially refunded');
  assert.equal(statusLabel('weird'), 'weird');
  assert.equal(statusClass('succeeded'), 'ok');
  assert.equal(statusClass('failed'), 'fail');
  assert.equal(statusClass('refunded'), 'warn');
  assert.equal(statusClass('partially_refunded'), 'partial');
  assert.equal(statusClass(''), '');
});

test('date presets and the filter query', () => {
  const today = new Date(Date.UTC(2026, 9, 22, 15, 0));
  assert.deepEqual(presetRange('7', today), { since: '2026-10-16', until: '2026-10-22' });
  assert.deepEqual(presetRange('30', today), { since: '2026-09-23', until: '2026-10-22' });
  assert.deepEqual(presetRange('365', today), { since: '2025-10-23', until: '2026-10-22' });
  assert.equal(presetRange('custom', today), null);
  assert.equal(filterQuery({}), '');
  assert.equal(
    filterQuery({ since: '2026-09-23', until: '2026-10-22', method: 'stripe', status: 'failed', q: 'a b@x', limit: 25, offset: 50 }),
    '?since=2026-09-23&until=2026-10-22&method=stripe&status=failed&q=a%20b%40x&limit=25&offset=50',
  );
  assert.equal(filterQuery({ method: '', status: '', offset: 0 }), '');
});

test('revenue chart geometry', () => {
  const byDay = [
    { key: '2026-10-01', revenue_cents: 0, net_cents: 0, refunded_cents: 0 },
    { key: '2026-10-02', revenue_cents: 2000, net_cents: 1000, refunded_cents: 1000 },
    { key: '2026-10-03', revenue_cents: 500, net_cents: 500, refunded_cents: 0 },
  ];
  const m = moneyChartModel(byDay, 400, 100, { top: 10, right: 10, bottom: 20, left: 40 });
  assert.equal(m.maxCents, 2000);
  assert.equal(m.dots.length, 3);
  assert.equal(m.dots[1].y, 10); // the maximum touches the top
  assert.equal(m.dots[0].y, 80); // zero sits on the baseline
  assert.equal(m.baseline, 80);
  assert.match(m.revenuePath, /^M40\.0 80\.0 L215\.0 10\.0 L390\.0 62\.5$/);
  assert.match(m.revenueArea, /Z$/);
  assert.match(m.refundsPath, /^M40\.0 80\.0 L215\.0 45\.0 L390\.0 80\.0$/);
  assert.deepEqual(m.ticks.map((t) => t.label), ['10-01', '10-02', '10-03']);
  const empty = moneyChartModel([], 400, 100);
  assert.equal(empty.revenuePath, '');
  assert.equal(empty.dots.length, 0);
  // Long windows thin the ticks out to about seven.
  const long = moneyChartModel(Array.from({ length: 90 }, (_, i) => ({ key: `2026-01-${i}`, revenue_cents: i })));
  assert.ok(long.ticks.length >= 7 && long.ticks.length <= 9);
});

test('share bars and donut segments', () => {
  const rows = shareBars([{ amount_cents: 50 }, { amount_cents: 200 }, { amount_cents: 0 }], 'amount_cents');
  assert.deepEqual(rows.map((r) => r.pct), [25, 100, 0]);
  const segs = donut([{ status: 'succeeded', count: 3 }, { status: 'failed', count: 1 }], 'count');
  assert.equal(segs[0].pct, 75);
  assert.equal(segs[0].dash, '75.00 25.00');
  assert.equal(segs[0].offset, '25.00');
  assert.equal(segs[1].pct, 25);
  assert.equal(segs[1].offset, '-50.00'); // starts where the first segment ends
  assert.deepEqual(donut([], 'count'), []);
  assert.equal(donut([{ count: 0 }], 'count')[0].pct, 0);
});

test('alert summary and trends', () => {
  assert.deepEqual(alertSummary([{ level: 'ok' }, { level: 'ok' }]), { level: 'ok', fails: 0, warns: 0, text: 'All 2 KPI checks pass' });
  assert.deepEqual(alertSummary([{ level: 'warn' }, { level: 'ok' }]), { level: 'warn', fails: 0, warns: 1, text: '1 warning' });
  assert.equal(alertSummary([{ level: 'fail' }, { level: 'fail' }, { level: 'warn' }]).text, '2 alerts · 1 warning');
  assert.equal(alertSummary(undefined).level, 'ok');
  assert.equal(trendText(0.125, 30), '+12.5 % vs. previous 30 days');
  assert.equal(trendText(-0.3, 7), '−30.0 % vs. previous 7 days');
  assert.equal(trendText(null, 30), 'no baseline (previous 30 days)');
  assert.equal(trendClass(0.1), 'up');
  assert.equal(trendClass(-0.1), 'down');
  assert.equal(trendClass(-0.1, true), 'up'); // fewer failures is good
  assert.equal(trendClass(0), '');
  assert.equal(trendClass(null), '');
});
