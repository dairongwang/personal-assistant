// Run: node --test apps/ads-admin/tests   (no dependencies)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  bars,
  campaignStatus,
  chartModel,
  ctr,
  fmtChange,
  fmtPct,
  kindLabel,
  overallStatus,
  regionName,
  settingsRows,
} from '../src/analytics.js';

test('ctr and formatting', () => {
  assert.equal(ctr(200, 5), 0.025);
  assert.equal(ctr(0, 5), 0);
  assert.equal(fmtPct(0.025), '2.5 %');
  assert.equal(fmtChange(0.125), '+12.5 %');
  assert.equal(fmtChange(-0.03), '−3.0 %');
  assert.equal(fmtChange(0), '±0.0 %');
  assert.equal(fmtChange(null), 'n/a');
  assert.equal(fmtChange(undefined), 'n/a');
});

test('labels', () => {
  assert.equal(kindLabel('adsense'), 'Google AdSense');
  assert.equal(kindLabel('weird'), 'weird');
  assert.equal(campaignStatus({ status: 'active', live: true }), 'live');
  assert.equal(campaignStatus({ status: 'active', live: false }), 'inactive');
  assert.equal(campaignStatus({ status: 'paused', live: false }), 'paused');
  assert.equal(regionName('DE', 'en'), 'Germany');
  assert.equal(regionName('other'), 'Other (small counts folded)');
  assert.equal(regionName('ZZ'), 'No region');
});

test('chart geometry', () => {
  const days = [
    { key: '2026-01-01', impressions: 10, clicks: 1 },
    { key: '2026-01-02', impressions: 20, clicks: 0 },
    { key: '2026-01-03', impressions: 0, clicks: 0 },
  ];
  const m = chartModel(days, 400, 100);
  assert.equal(m.maxImpressions, 20);
  assert.equal(m.maxClicks, 1);
  assert.match(m.impressionsPath, /^M40\.0 \d+\.\d L/);
  assert.ok(m.impressionsArea.endsWith('Z'));
  assert.equal(m.dots.length, 3);
  assert.equal(m.ticks.length, 3);
  assert.deepEqual(m.ticks.map((t) => t.label), ['01-01', '01-02', '01-03']);
  // Highest value sits at the top padding, zero on the baseline.
  assert.equal(m.dots[1].y, 10);
  assert.equal(m.dots[2].y, m.baseline);
  // Degenerate inputs never throw.
  assert.equal(chartModel([]).impressionsPath, '');
  assert.equal(chartModel(null).dots.length, 0);
  assert.ok(chartModel(Array.from({ length: 30 }, (_, i) => ({ key: `2026-01-${i + 1}`, impressions: i }))).ticks.length <= 9);
});

test('bars and statuses', () => {
  assert.deepEqual(
    bars([{ key: 'DE', impressions: 50 }, { key: 'FR', impressions: 25 }]).map((b) => b.pct),
    [100, 50],
  );
  assert.equal(overallStatus([{ status: 'ok' }, { status: 'warn' }]), 'warn');
  assert.equal(overallStatus([{ status: 'ok' }, { status: 'fail' }, { status: 'warn' }]), 'fail');
  assert.equal(overallStatus([]), 'ok');
  assert.deepEqual(settingsRows({ client: 'ca-pub-1' }), [{ key: 'client', value: 'ca-pub-1' }]);
  assert.deepEqual(settingsRows(undefined), []);
});
