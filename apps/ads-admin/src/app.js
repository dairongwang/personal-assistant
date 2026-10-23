/* Ad-admin dashboard — a small hash-routed single-page app with no build step and no
 * dependencies. Views are rendered from template strings; every dynamic value goes through
 * esc() (text) or attr()/safeUrl() (attributes), so API data can never inject markup. */

import {
  bars,
  campaignStatus,
  chartModel,
  fmtChange,
  fmtInt,
  fmtPct,
  kindLabel,
  overallStatus,
  regionName,
  settingsRows,
} from './analytics.js';
import { ApiError, api, apiBase, session } from './api.js';
import {
  PRESETS,
  STATUSES,
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
} from './payments.js';

const root = document.getElementById('app');
const state = {
  me: null,
  days: 30,
  kind: '',
  includeArchived: false,
  editing: null,
  selected: null,
  // Payments section: filters (date range preset or custom since/until, method, status), the
  // drill-down list (search, paging) and the transaction opened in the detail panel.
  pay: { preset: '30', ...presetRange('30'), method: '', status: '', q: '', offset: 0, limit: 25, open: null },
};

// ---------------------------------------------------------------------------- utilities

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
const attr = esc;
function safeUrl(url) {
  return /^https:\/\//i.test(url || '') ? url : null;
}
function badge(text, cls = '') {
  return `<span class="badge ${attr(cls)}">${esc(text)}</span>`;
}
function date(iso) {
  return iso ? new Date(iso).toLocaleString() : '—';
}
function toast(message, error = false) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.setAttribute('role', 'status');
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove('show'), 3200);
}

// ---------------------------------------------------------------------------- shell

const NAV = [
  ['overview', 'Overview'],
  ['payments', 'Payments'],
  ['campaigns', 'Campaigns'],
  ['sources', 'Ad source'],
  ['privacy', 'Privacy & compliance'],
  ['audit', 'Audit log'],
];

function route() {
  const name = (location.hash || '#/overview').replace(/^#\/?/, '').split('?')[0] || 'overview';
  return NAV.some(([n]) => n === name) ? name : 'overview';
}

function shell(content, active) {
  return `
    <header class="top">
      <div class="brand"><span class="logo">AD</span><span><strong>Ads admin</strong><br /><span class="muted small">Personal Assistant · operations console: advertising &amp; payments</span></span></div>
      <nav class="nav" aria-label="Sections">
        ${NAV.map(([n, label]) => `<a href="#/${n}" class="${n === active ? 'active' : ''}" data-nav="${n}">${esc(label)}</a>`).join('')}
      </nav>
      <div class="who">
        <span class="small muted" id="who">${esc(state.me?.email ?? '')} · admin</span>
        <button class="btn" id="btn-logout" data-action="logout">Sign out</button>
      </div>
    </header>
    <main class="main" id="main">${content}</main>
    <footer class="foot muted small">API: ${esc(apiBase())} · this console stores only your session token, in this tab.</footer>`;
}

function render(html) {
  root.innerHTML = html;
}

function errorBox(e) {
  return `<div class="callout error" role="alert" id="error">${esc(e instanceof Error ? e.message : String(e))}</div>`;
}

// ---------------------------------------------------------------------------- sign in

function loginView(error = '') {
  return `
    <div class="login">
      <form id="login-form" class="card login-card">
        <div class="brand big"><span class="logo">AD</span><strong>Ads admin</strong></div>
        <p class="muted small">Advertising management and analytics for Personal Assistant. Administrator accounts only. Your password is turned into a login key in this browser and never sent.</p>
        ${error ? `<div class="callout error" role="alert" id="login-error">${esc(error)}</div>` : ''}
        <label>E-mail <input class="input" id="login-email" name="email" type="email" required autocomplete="username" /></label>
        <label>Password <input class="input" id="login-password" name="password" type="password" required autocomplete="current-password" /></label>
        <button class="btn primary" id="login-submit" type="submit">Sign in</button>
        <p class="muted small">API: ${esc(apiBase())}</p>
      </form>
    </div>`;
}

async function showLogin(error = '') {
  render(loginView(error));
  document.getElementById('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = document.getElementById('login-submit');
    btn.disabled = true;
    btn.textContent = 'Deriving key…';
    document.getElementById('login-error')?.remove();
    try {
      state.me = await api.login(
        document.getElementById('login-email').value.trim(),
        document.getElementById('login-password').value,
      );
      location.hash = '#/overview';
      await show();
    } catch (e) {
      await showLogin(e.message);
    }
  });
}

// ---------------------------------------------------------------------------- overview

function kpi(label, value, sub = '', id = '') {
  return `<div class="kpi" ${id ? `id="${attr(id)}"` : ''}><span class="muted small">${esc(label)}</span><strong>${value}</strong><span class="small muted">${sub}</span></div>`;
}

function lineChart(byDay) {
  const m = chartModel(byDay, 720, 200);
  return `
    <svg class="chart" viewBox="0 0 ${m.width} ${m.height}" role="img" aria-label="Impressions and clicks per day" id="chart">
      <line x1="40" y1="${m.baseline}" x2="${m.width - 12}" y2="${m.baseline}" class="axis" />
      <text x="4" y="14" class="axis-label">${fmtInt(m.maxImpressions)}</text>
      <text x="4" y="${m.baseline}" class="axis-label">0</text>
      <path d="${attr(m.impressionsArea)}" class="area" />
      <path d="${attr(m.impressionsPath)}" class="line imp" />
      <path d="${attr(m.clicksPath)}" class="line clk" />
      ${m.dots.map((d) => `<circle cx="${d.x.toFixed(1)}" cy="${d.y.toFixed(1)}" r="3" class="dot"><title>${esc(d.p.key)}: ${fmtInt(d.p.impressions)} views · ${fmtInt(d.p.clicks)} clicks</title></circle>`).join('')}
      ${m.ticks.map((t) => `<text x="${t.x.toFixed(1)}" y="${m.height - 6}" class="axis-label" text-anchor="middle">${esc(t.label)}</text>`).join('')}
    </svg>
    <p class="legend small muted"><span class="sw imp"></span> impressions (left scale, max ${fmtInt(m.maxImpressions)}) <span class="sw clk"></span> clicks (own scale, max ${fmtInt(m.maxClicks)})</p>`;
}

function barList(items, labelFn, id) {
  const rows = bars(items);
  if (!rows.length) return `<p class="muted small">No data in this window.</p>`;
  return `<ul class="bars" id="${attr(id)}">${rows
    .map(
      (r) => `<li><span class="bar-label">${esc(labelFn(r))}</span><span class="bar"><span class="bar-fill" style="width:${r.pct}%"></span></span><span class="bar-value">${fmtInt(r.impressions)} · ${fmtInt(r.clicks)} · ${fmtPct(r.impressions ? r.clicks / r.impressions : 0)}</span></li>`,
    )
    .join('')}</ul>`;
}

function overviewView(a) {
  const src = a.active_source;
  return `
    <div class="toolbar">
      <h1>Overview</h1>
      <span class="spacer"></span>
      <label class="small muted">Window
        <select class="input" id="days" data-action="days">
          ${[7, 14, 30, 90, 365].map((d) => `<option value="${d}" ${d === state.days ? 'selected' : ''}>${d} days</option>`).join('')}
        </select></label>
      <label class="small muted">Kind
        <select class="input" id="kind" data-action="kind">
          <option value="">all first-party</option>
          ${['internal', 'sponsored', 'affiliate'].map((k) => `<option value="${k}" ${k === state.kind ? 'selected' : ''}>${esc(kindLabel(k))}</option>`).join('')}
        </select></label>
      <button class="btn" id="btn-csv" data-action="csv">Export CSV</button>
    </div>
    <div class="kpis">
      ${kpi('Impressions', fmtInt(a.impressions), `${esc(fmtChange(a.impressions_change))} vs. previous ${a.days} days`, 'kpi-impressions')}
      ${kpi('Clicks', fmtInt(a.clicks), `${esc(fmtChange(a.clicks_change))} vs. previous ${a.days} days`, 'kpi-clicks')}
      ${kpi('CTR', fmtPct(a.ctr, 2), 'clicks ÷ impressions', 'kpi-ctr')}
      ${kpi('Active source', esc(src.title), `${esc(src.mode)} · ${src.third_party ? 'third party (consent-gated)' : src.kind === 'network' ? 'server-to-server' : 'first-party'}`, 'kpi-source')}
    </div>
    <section class="card"><h2>Per day <span class="muted small">${esc(a.since)} → ${esc(a.until)}</span></h2>${lineChart(a.by_day)}</section>
    <div class="grid2">
      <section class="card"><h2>By source kind</h2>${barList(a.by_kind.map((k) => ({ ...k, key: k.kind })), (r) => `${kindLabel(r.kind)} (${r.campaigns})`, 'by-kind')}</section>
      <section class="card"><h2>By country <span class="muted small">counts under ${a.min_count} folded into “other” (${a.regions_folded} folded)</span></h2>${barList(a.by_region, (r) => `${regionName(r.key)} ${r.key !== 'other' ? `(${r.key})` : ''}`, 'by-region')}</section>
    </div>
    <section class="card">
      <h2>Campaign performance</h2>
      <table class="table" id="perf">
        <thead><tr><th>Campaign</th><th>Kind</th><th>Status</th><th class="num">Impressions</th><th class="num">Clicks</th><th class="num">CTR</th><th class="num">Share</th><th class="num">All-time</th></tr></thead>
        <tbody>${a.campaigns
          .map(
            (c) => `<tr data-id="${attr(c.id)}"><td><strong>${esc(c.sponsor)}</strong><br /><span class="small muted">${esc(c.headline)}</span></td><td>${badge(kindLabel(c.kind), 'kind')}</td><td>${badge(campaignStatus(c), campaignStatus(c))}</td><td class="num">${fmtInt(c.impressions)}</td><td class="num">${fmtInt(c.clicks)}</td><td class="num">${fmtPct(c.ctr, 2)}</td><td class="num">${fmtPct(c.share)}</td><td class="num small muted">${fmtInt(c.impressions_total)} · ${fmtInt(c.clicks_total)}</td></tr>`,
          )
          .join('')}</tbody>
      </table>
      <p class="muted small">${esc(a.third_party_note)}</p>
    </section>`;
}

// ---------------------------------------------------------------------------- payments

function moneyChart(byDay, currency) {
  const m = moneyChartModel(byDay, 720, 200);
  return `
    <svg class="chart money" viewBox="0 0 ${m.width} ${m.height}" role="img" aria-label="Revenue, net revenue and refunds per day" id="revenue-chart">
      <line x1="${m.left}" y1="${m.baseline}" x2="${m.width - 12}" y2="${m.baseline}" class="axis" />
      <text x="4" y="14" class="axis-label">${esc(fmtCents(m.maxCents, currency))}</text>
      <text x="4" y="${m.baseline}" class="axis-label">${esc(fmtCents(0, currency))}</text>
      <path d="${attr(m.revenueArea)}" class="area" />
      <path d="${attr(m.revenuePath)}" class="line gross" />
      <path d="${attr(m.netPath)}" class="line net" />
      <path d="${attr(m.refundsPath)}" class="line refund" />
      ${m.dots.map((d) => `<circle cx="${d.x.toFixed(1)}" cy="${d.y.toFixed(1)}" r="3" class="dot"><title>${esc(d.p.key)}: ${esc(fmtCents(d.p.revenue_cents, currency))} gross · ${esc(fmtCents(d.p.refunded_cents, currency))} refunded · ${d.p.succeeded} paid · ${d.p.failed} failed</title></circle>`).join('')}
      ${m.ticks.map((t) => `<text x="${t.x.toFixed(1)}" y="${m.height - 6}" class="axis-label" text-anchor="middle">${esc(t.label)}</text>`).join('')}
    </svg>
    <p class="legend small muted"><span class="sw gross"></span> gross revenue <span class="sw net"></span> net (gross − refunds) <span class="sw refund"></span> refunds (by refund date)</p>`;
}

function donutChart(items, key, labelFn, valueFn, id) {
  const segs = donut(items, key);
  if (!segs.length) return `<p class="muted small">No data in this window.</p>`;
  return `
    <div class="donut-wrap" id="${attr(id)}">
      <svg class="donut" viewBox="0 0 42 42" role="img" aria-label="Share chart">
        <circle cx="21" cy="21" r="15.915" class="ring" />
        ${segs.map((s, i) => `<circle cx="21" cy="21" r="15.915" class="seg c${i % 6} ${attr(statusClass(s.status || ''))}" stroke-dasharray="${s.dash}" stroke-dashoffset="${s.offset}"><title>${esc(labelFn(s))}: ${s.pct.toFixed(1)} %</title></circle>`).join('')}
      </svg>
      <ul class="donut-legend">${segs.map((s, i) => `<li><span class="sw c${i % 6} ${attr(statusClass(s.status || ''))}"></span>${esc(labelFn(s))} <span class="muted small">${s.pct.toFixed(1)} % · ${esc(valueFn(s))}</span></li>`).join('')}</ul>
    </div>`;
}

function moneyBars(items, key, labelFn, valueFn, id) {
  const rows = shareBars(items, key);
  if (!rows.length) return `<p class="muted small">No data in this window.</p>`;
  return `<ul class="bars" id="${attr(id)}">${rows
    .map((r) => `<li><span class="bar-label">${esc(labelFn(r))}</span><span class="bar"><span class="bar-fill" style="width:${r.pct}%"></span></span><span class="bar-value">${esc(valueFn(r))}</span></li>`)
    .join('')}</ul>`;
}

function kpiTrend(label, value, change, days, sub, id, invert = false) {
  return `<div class="kpi" id="${attr(id)}"><span class="muted small">${esc(label)}</span><strong>${value}</strong><span class="small trend ${attr(trendClass(change, invert))}">${esc(trendText(change, days))}</span>${sub ? `<span class="small muted">${sub}</span>` : ''}</div>`;
}

function alertsStrip(alerts) {
  const s = alertSummary(alerts);
  return `
    <section class="card alerts ${attr(s.level)}" id="kpi-alerts">
      <h2>KPI alerts ${badge(s.text, s.level)}</h2>
      <ul class="checks">${alerts
        .map((a) => `<li class="check ${attr(a.level)}" data-key="${attr(a.key)}"><span class="dot"></span><span><strong>${esc(a.title)}</strong><br /><span class="small muted">${esc(a.detail)}</span></span>${badge(a.level === 'ok' ? 'ok' : a.level === 'warn' ? 'warning' : 'alert', a.level)}</li>`)
        .join('')}</ul>
    </section>`;
}

function transactionRow(t, currency) {
  const open = state.pay.open === t.id;
  return `<tr data-id="${t.id}" class="tx ${open ? 'selected' : ''}" data-action="tx" tabindex="0">
    <td class="small">${esc(date(t.paid_at))}</td>
    <td><strong>${esc(t.email)}</strong><br /><span class="small muted mono">${esc(t.provider_payment_id)}</span></td>
    <td>${badge(t.provider, 'kind')}</td><td class="small">${esc(planName(t.plan))}</td>
    <td class="num">${esc(fmtMoney(t.amount, t.currency || currency))}</td>
    <td>${badge(statusLabel(t.status), statusClass(t.status))}${t.status === 'failed' && t.failure_reason ? `<br /><span class="small muted">${esc(t.failure_reason)}</span>` : ''}</td>
    <td class="num">${Number(t.refunded) ? esc(fmtMoney(t.refunded, t.currency || currency)) : '—'}</td>
  </tr>`;
}

function planName(plan) {
  return { pro_monthly: 'Pro — monthly', pro_yearly: 'Pro — yearly', lifetime: 'Lifetime' }[plan] ?? plan;
}

function transactionDetail(d, currency) {
  const t = d.transaction;
  const a = d.account;
  const row = (k, v) => `<div><span class="muted small">${esc(k)}</span><span>${v}</span></div>`;
  return `
    <section class="card" id="tx-detail">
      <div class="toolbar"><h2>Transaction #${t.id} <span class="muted small mono">${esc(t.provider_payment_id)}</span></h2><span class="spacer"></span><button class="btn sm" data-action="tx-close">Close</button></div>
      <div class="grid2">
        <div class="facts">
          <h3>Payment</h3>
          ${row('Paid at', esc(date(t.paid_at)))}
          ${row('Amount', `${esc(fmtMoney(t.amount, t.currency || currency))} · ${esc(planName(t.plan))} via ${badge(t.provider, 'kind')}`)}
          ${row('Status', `${badge(statusLabel(t.status), statusClass(t.status))}${t.failure_reason ? ` <span class="small muted">${esc(t.failure_reason)}</span>` : ''}`)}
          ${row('Refunded', Number(t.refunded) ? `${esc(fmtMoney(t.refunded, t.currency || currency))} on ${esc(date(t.refunded_at))} <span class="small muted mono">${esc(t.refund_id ?? '')}</span>` : '—')}
          ${row('Period paid for', t.period_start ? `${esc(date(t.period_start))} → ${esc(date(t.period_end))}` : 'one-off (Lifetime)')}
        </div>
        <div class="facts">
          <h3>Customer</h3>
          ${row('Account', `<strong>${esc(a.email)}</strong> <span class="small muted mono">${esc(a.id)}</span>${a.deleted_at ? ' ' + badge('deleted', 'fail') : ''}`)}
          ${row('Membership', `${badge(a.membership, a.membership === 'paid' ? 'ok' : a.membership === 'admin' ? 'kind' : '')} ${esc(a.plan)} · ${esc(a.status)}${a.current_period_end ? ` · until ${esc(date(a.current_period_end))}` : ''}`)}
          ${row('Provider ids', `<span class="small mono">${esc(a.provider)} · ${esc(a.provider_customer_id ?? '—')} · ${esc(a.provider_subscription_id ?? '—')}</span>`)}
          ${row('Lifetime value', `${esc(fmtCents(a.lifetime_value_cents, currency))} <span class="small muted">(paid − refunded, all time)</span>`)}
          ${row('Customer since', esc(date(a.created_at)))}
        </div>
      </div>
      <h3>All payments of this customer</h3>
      <table class="table" id="tx-history"><thead><tr><th>When</th><th>Id</th><th>Plan</th><th class="num">Amount</th><th>Status</th><th class="num">Refunded</th></tr></thead>
      <tbody>${d.history.map((h) => `<tr class="${h.id === t.id ? 'selected' : ''}"><td class="small">${esc(date(h.paid_at))}</td><td class="small mono">${esc(h.provider_payment_id)}</td><td class="small">${esc(planName(h.plan))}</td><td class="num">${esc(fmtMoney(h.amount, h.currency || currency))}</td><td>${badge(statusLabel(h.status), statusClass(h.status))}</td><td class="num">${Number(h.refunded) ? esc(fmtMoney(h.refunded, h.currency || currency)) : '—'}</td></tr>`).join('')}</tbody></table>
    </section>`;
}

function paymentsView(a, page, detail) {
  const f = state.pay;
  const cur = a.currency;
  const pages = Math.max(1, Math.ceil(page.total / f.limit));
  const current = Math.floor(f.offset / f.limit) + 1;
  return `
    <div class="toolbar">
      <h1>Payments</h1>
      <span class="spacer"></span>
      <button class="btn" id="btn-pay-csv" data-action="pay-csv">Export CSV</button>
    </div>
    <form class="card filters" id="pay-filters">
      <label class="small muted">Range
        <select class="input" name="preset" id="pay-preset" data-action="pay-preset">${PRESETS.map(([v, l]) => `<option value="${v}" ${f.preset === v ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
      <label class="small muted">From <input class="input" type="date" name="since" id="pay-since" value="${attr(f.since)}" ${f.preset === 'custom' ? '' : 'disabled'} /></label>
      <label class="small muted">To <input class="input" type="date" name="until" id="pay-until" value="${attr(f.until)}" ${f.preset === 'custom' ? '' : 'disabled'} /></label>
      <label class="small muted">Method
        <select class="input" name="method" id="pay-method"><option value="">all methods</option>${a.methods.map((m) => `<option value="${attr(m)}" ${f.method === m ? 'selected' : ''}>${esc(a.by_method.find((x) => x.provider === m)?.label ?? m)}</option>`).join('')}</select></label>
      <label class="small muted">Status
        <select class="input" name="status" id="pay-status"><option value="">all statuses</option>${STATUSES.map((st) => `<option value="${st}" ${f.status === st ? 'selected' : ''}>${esc(statusLabel(st))}</option>`).join('')}</select></label>
      <button class="btn primary" type="submit" id="pay-apply">Apply</button>
      <span class="small muted" id="pay-window">${esc(a.since)} → ${esc(a.until)} · ${a.days} days${a.mixed_currencies ? ' · ⚠ mixed currencies, totals are nominal' : ''}</span>
    </form>
    ${alertsStrip(a.alerts)}
    <div class="kpis">
      ${kpiTrend('Net revenue', esc(fmtMoney(a.net_revenue, cur)), a.revenue_change, a.days, `gross ${esc(fmtMoney(a.revenue, cur))} − refunds ${esc(fmtMoney(a.refunded, cur))}`, 'kpi-revenue')}
      ${kpiTrend('Transactions', fmtInt(a.transactions), a.transactions_change, a.days, '', 'kpi-transactions')}
      ${kpi('Successful / failed', `${fmtInt(a.succeeded)} <span class="muted">/</span> <span class="${a.failed ? 'fail-text' : ''}">${fmtInt(a.failed)}</span>`, `success rate ${esc(fmtPct(a.success_rate))}`, 'kpi-success')}
      ${kpi('Refunds', `${fmtInt(a.refund_count)} <span class="muted small">· ${esc(fmtMoney(a.refunded, cur))}</span>`, `refund rate ${esc(fmtPct(a.refund_rate))} of gross`, 'kpi-refunds')}
      ${kpi('Average transaction', esc(fmtMoney(a.average_transaction, cur)), 'gross ÷ successful payments', 'kpi-average')}
    </div>
    <section class="card"><h2>Revenue trend <span class="muted small">per day</span></h2>${moneyChart(a.by_day, cur)}</section>
    <div class="grid2">
      <section class="card"><h2>Payment status</h2>${donutChart(a.by_status, 'count', (s) => statusLabel(s.status), (s) => `${s.count} · ${fmtMoney(s.amount, cur)}`, 'by-status')}</section>
      <section class="card"><h2>Payment method <span class="muted small">gross revenue</span></h2>${donutChart(a.by_method, 'amount_cents', (m) => m.label, (m) => `${fmtMoney(m.amount, cur)} · ${m.succeeded} paid · ${m.failed} failed · ${fmtMoney(m.refunded, cur)} refunded`, 'by-method')}</section>
    </div>
    <div class="grid2">
      <section class="card"><h2>Top customers <span class="muted small">by gross revenue in the window</span></h2>
        ${a.top_customers.length ? `<table class="table" id="top-customers"><thead><tr><th>Customer</th><th class="num">Payments</th><th class="num">Gross</th><th class="num">Refunded</th><th class="num">Share</th></tr></thead><tbody>${a.top_customers
          .map((c) => `<tr data-action="pay-search" data-q="${attr(c.email)}" class="tx"><td><strong>${esc(c.email)}</strong> ${badge(c.membership, c.membership === 'paid' ? 'ok' : c.membership === 'admin' ? 'kind' : '')}<br /><span class="bar"><span class="bar-fill" style="width:${Math.round(100 * c.share)}%"></span></span></td><td class="num">${c.payments}</td><td class="num">${esc(fmtMoney(c.amount, cur))}</td><td class="num">${Number(c.refunded) ? esc(fmtMoney(c.refunded, cur)) : '—'}</td><td class="num">${esc(fmtPct(c.share))}</td></tr>`)
          .join('')}</tbody></table>` : '<p class="muted small">No paying customers in this window.</p>'}
      </section>
      <section class="card"><h2>By plan</h2>${moneyBars(a.by_plan, 'amount_cents', (p) => p.title, (p) => `${fmtMoney(p.amount, cur)} · ${p.count} · ${fmtPct(p.share)}`, 'by-plan')}</section>
    </div>
    <section class="card"><h2>Refunds <span class="muted small">${a.refund_count} in the window · ${esc(fmtMoney(a.refunded, cur))}</span></h2>
      ${a.refunds.length ? `<table class="table" id="refunds"><thead><tr><th>Refunded at</th><th>Customer</th><th>Plan</th><th class="num">Paid</th><th class="num">Refunded</th><th>Kind</th><th>Refund id</th></tr></thead><tbody>${a.refunds
        .map((r) => `<tr data-action="tx" data-id="${r.id}" class="tx"><td class="small">${esc(date(r.refunded_at))}</td><td>${esc(r.email)}</td><td class="small">${esc(planName(r.plan))}</td><td class="num">${esc(fmtMoney(r.amount, cur))}</td><td class="num">${esc(fmtMoney(r.refunded, cur))}</td><td>${badge(r.full ? 'full' : 'partial', r.full ? 'warn' : '')}</td><td class="small mono">${esc(r.refund_id ?? '—')}</td></tr>`)
        .join('')}</tbody></table>` : '<p class="muted small">No refunds in this window.</p>'}
    </section>
    ${detail ? transactionDetail(detail, cur) : ''}
    <section class="card">
      <div class="toolbar"><h2>Transactions <span class="muted small">${fmtInt(page.total)} in the window · click a row for details</span></h2><span class="spacer"></span>
        <form id="pay-search-form" class="inline"><input class="input" type="search" name="q" id="pay-q" value="${attr(f.q)}" placeholder="e-mail, payment id or refund id" /><button class="btn" type="submit">Search</button></form></div>
      <table class="table" id="transactions"><thead><tr><th>When</th><th>Customer · id</th><th>Method</th><th>Plan</th><th class="num">Amount</th><th>Status</th><th class="num">Refunded</th></tr></thead>
      <tbody>${page.items.map((t) => transactionRow(t, cur)).join('')}</tbody></table>
      ${page.items.length ? '' : '<p class="muted small">No transactions match these filters.</p>'}
      <div class="toolbar pager"><span class="small muted">Page ${current} of ${pages}</span><span class="spacer"></span>
        <button class="btn sm" data-action="pay-page" data-offset="${Math.max(0, f.offset - f.limit)}" ${f.offset === 0 ? 'disabled' : ''}>‹ Previous</button>
        <button class="btn sm" data-action="pay-page" data-offset="${f.offset + f.limit}" ${f.offset + f.limit >= page.total ? 'disabled' : ''}>Next ›</button></div>
    </section>`;
}

function payQuery(extra = {}) {
  const f = state.pay;
  return filterQuery({ since: f.since, until: f.until, method: f.method, status: f.status, ...extra });
}

async function loadPayments() {
  const f = state.pay;
  const [analytics, page, detail] = await Promise.all([
    api.paymentAnalytics(payQuery()),
    api.paymentTransactions(payQuery({ q: f.q, limit: f.limit, offset: f.offset })),
    f.open ? api.paymentTransaction(f.open).catch(() => null) : Promise.resolve(null),
  ]);
  return paymentsView(analytics, page, detail);
}

// ---------------------------------------------------------------------------- campaigns

const EMPTY = {
  kind: 'sponsored',
  sponsor: '',
  headline: '',
  blurb: '',
  cta_label: 'Learn more',
  target_url: '',
  image_url: '',
  regions: '',
  languages: '',
  status: 'draft',
  weight: 10,
  starts_at: '',
  ends_at: '',
  max_impressions: '',
};

function field(label, name, value, attrs = '', hint = '') {
  return `<label class="field"><span>${esc(label)}</span><input class="input" name="${attr(name)}" value="${attr(value ?? '')}" ${attrs} />${hint ? `<span class="small muted">${esc(hint)}</span>` : ''}</label>`;
}

function campaignForm(c) {
  const isNew = !state.editing?.id;
  return `
    <form class="card form" id="campaign-form">
      <h2>${isNew ? 'New campaign' : 'Edit campaign'}</h2>
      <div class="grid3">
        <label class="field"><span>Kind</span><select class="input" name="kind">${['sponsored', 'internal', 'affiliate'].map((k) => `<option value="${k}" ${c.kind === k ? 'selected' : ''}>${esc(kindLabel(k))}</option>`).join('')}</select></label>
        ${field('Sponsor', 'sponsor', c.sponsor, 'required maxlength="80"')}
        ${field('Button text', 'cta_label', c.cta_label, 'required maxlength="30"')}
      </div>
      ${field('Headline', 'headline', c.headline, 'required maxlength="90"')}
      <label class="field"><span>Text</span><textarea class="input" name="blurb" maxlength="200" rows="2">${esc(c.blurb)}</textarea></label>
      <div class="grid2">
        ${field('Link (https, or app:upgrade for internal ads)', 'target_url', c.target_url, 'required')}
        ${field('Image URL (optional, first-party host)', 'image_url', c.image_url || '')}
      </div>
      <div class="grid3">
        ${field('Regions', 'regions', c.regions, 'placeholder="everywhere"', 'DE, US or groups EU, DACH, NA …; empty = everywhere')}
        ${field('Languages', 'languages', c.languages, 'placeholder="any"', 'en, de …; empty = any')}
        ${field('Weight (1–100)', 'weight', c.weight, 'type="number" min="1" max="100"')}
      </div>
      <div class="grid3">
        <label class="field"><span>Status</span><select class="input" name="status">${['draft', 'active', 'paused'].map((s) => `<option value="${s}" ${c.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
        ${field('Starts (optional)', 'starts_at', toLocal(c.starts_at), 'type="datetime-local"')}
        ${field('Ends (optional)', 'ends_at', toLocal(c.ends_at), 'type="datetime-local"')}
      </div>
      ${field('Maximum views (optional budget cap)', 'max_impressions', c.max_impressions ?? '', 'type="number" min="1"')}
      <div class="actions"><button class="btn" type="button" data-action="cancel-edit">Cancel</button><span class="spacer"></span><button class="btn primary" type="submit" id="campaign-save">${isNew ? 'Create campaign' : 'Save changes'}</button></div>
    </form>`;
}

function toLocal(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function campaignsView(list, detail) {
  return `
    <div class="toolbar">
      <h1>Campaigns</h1>
      <span class="spacer"></span>
      <label class="small muted"><input type="checkbox" id="archived" data-action="archived" ${state.includeArchived ? 'checked' : ''} /> show archived</label>
      <button class="btn primary" id="btn-new" data-action="new">+ New campaign</button>
    </div>
    ${state.editing ? campaignForm(state.editing) : ''}
    <section class="card">
      <table class="table" id="campaigns">
        <thead><tr><th>Campaign</th><th>Kind</th><th>Status</th><th>Targeting</th><th class="num">Weight</th><th class="num">All-time views · clicks</th><th></th></tr></thead>
        <tbody>${list
          .map(
            (c) => `<tr data-id="${attr(c.id)}" class="${state.selected === c.id ? 'selected' : ''}">
              <td><strong>${esc(c.sponsor)}</strong><br /><span class="small muted">${esc(c.headline)}</span></td>
              <td>${badge(kindLabel(c.kind), 'kind')}</td><td>${badge(campaignStatus(c), campaignStatus(c))}</td>
              <td class="small">${esc(c.regions || 'everywhere')}${c.languages ? ` · ${esc(c.languages)}` : ''}</td>
              <td class="num">${c.weight}</td><td class="num">${fmtInt(c.impressions_total)} · ${fmtInt(c.clicks_total)}</td>
              <td class="row-actions">
                <button class="btn sm" data-action="stats" data-id="${attr(c.id)}">Stats</button>
                ${c.status !== 'archived' ? `<button class="btn sm" data-action="edit" data-id="${attr(c.id)}">Edit</button>` : ''}
                ${c.status === 'active' ? `<button class="btn sm" data-action="pause" data-id="${attr(c.id)}">Pause</button>` : c.status !== 'archived' ? `<button class="btn sm" data-action="activate" data-id="${attr(c.id)}">Activate</button>` : ''}
                ${c.status !== 'archived' ? `<button class="btn sm danger" data-action="archive" data-id="${attr(c.id)}">Archive</button>` : ''}
              </td></tr>`,
          )
          .join('')}</tbody>
      </table>
      ${list.length ? '' : '<p class="muted">No campaigns yet.</p>'}
    </section>
    ${detail ? campaignDetail(detail) : ''}`;
}

function campaignDetail({ campaign: c, stats: s }) {
  return `
    <section class="card" id="campaign-stats">
      <h2>${esc(c.sponsor)} — ${esc(c.headline)} <span class="muted small">last ${s.days} days</span></h2>
      <div class="kpis">${kpi('Impressions', fmtInt(s.impressions))}${kpi('Clicks', fmtInt(s.clicks))}${kpi('CTR', fmtPct(s.ctr, 2))}${kpi('Link', safeUrl(c.target_url) ? `<a href="${attr(c.target_url)}" target="_blank" rel="noopener noreferrer">open ↗</a>` : esc(c.target_url))}</div>
      ${lineChart(s.by_day)}
      <h3>By country</h3>
      ${barList(s.by_region, (r) => `${regionName(r.key)} (${r.key})`, 'campaign-regions')}
      <p class="muted small">Per-campaign figures are raw daily totals per country; the Overview and the CSV export fold small counts into “other”.</p>
    </section>`;
}

function formToBody(form) {
  const f = new FormData(form);
  const v = (k) => String(f.get(k) ?? '').trim();
  return {
    kind: v('kind'),
    sponsor: v('sponsor'),
    headline: v('headline'),
    blurb: v('blurb'),
    cta_label: v('cta_label'),
    target_url: v('target_url'),
    image_url: v('image_url') || null,
    regions: v('regions'),
    languages: v('languages'),
    status: v('status'),
    weight: Math.min(100, Math.max(1, Number(v('weight')) || 10)),
    starts_at: v('starts_at') ? new Date(v('starts_at')).toISOString() : null,
    ends_at: v('ends_at') ? new Date(v('ends_at')).toISOString() : null,
    max_impressions: v('max_impressions') ? Number(v('max_impressions')) : null,
  };
}

// ---------------------------------------------------------------------------- sources

function sourcesView(s) {
  return `
    <div class="toolbar"><h1>Ad source</h1></div>
    <p class="muted">Where the user app's panel gets its content. One source is active at a time; a switch reaches every device within a minute and is audited. ${s.fakes_enabled ? 'Built-in fakes stand in for providers without credentials (ADS_FAKE_PROVIDERS).' : 'Fakes are off: providers need credentials (ADS_* variables).'}</p>
    <div class="sources" id="sources">
      ${s.sources
        .map(
          (o) => `<label class="source ${o.active ? 'active' : ''} ${o.mode === 'unavailable' ? 'unavailable' : ''}" data-kind="${attr(o.kind)}">
            <input type="radio" name="source" value="${attr(o.kind)}" ${o.active ? 'checked' : ''} ${o.mode === 'unavailable' ? 'disabled' : ''} data-action="source" id="source-${attr(o.kind)}" />
            <span>
              <strong>${esc(o.title)} ${badge(o.mode, o.mode)} ${o.third_party ? badge('third party · consent', 'warn') : o.kind === 'network' && o.render === 'creatives' ? badge('server-to-server', 'ok') : badge('first-party', 'ok')}</strong>
              <span class="small muted">${esc(o.reason || o.reporting)}</span>
              <span class="chips">${settingsRows(o.settings).map((r) => `<span class="chip">${esc(r.key)}: ${esc(r.value)}</span>`).join('')}</span>
            </span></label>`,
        )
        .join('')}
    </div>`;
}

// ---------------------------------------------------------------------------- privacy & compliance

function privacyView(c) {
  const overall = overallStatus(c.checks);
  return `
    <div class="toolbar"><h1>Privacy & compliance</h1><span class="spacer"></span>${badge(overall === 'ok' ? 'all checks pass' : overall === 'warn' ? 'attention needed' : 'action required', overall)} <span class="small muted">generated ${esc(date(c.generated_at))} · ${esc(c.environment)}</span></div>
    <p class="muted">${esc(c.disclaimer)}</p>
    <section class="card">
      <h2>Live checks <span class="muted small">against the running configuration and database</span></h2>
      <ul class="checks" id="checks">${c.checks
        .map((k) => `<li class="check ${attr(k.status)}" data-key="${attr(k.key)}"><span class="dot"></span><span><strong>${esc(k.title)}</strong><br /><span class="small muted">${esc(k.detail)}</span></span>${badge(k.status, k.status)}</li>`)
        .join('')}</ul>
    </section>
    <section class="card">
      <h2>Data inventory <span class="muted small">everything the advertising feature keeps, and where</span></h2>
      <table class="table" id="inventory"><thead><tr><th>Data</th><th>Where</th><th>Personal?</th><th>Retention</th><th class="num">Rows</th></tr></thead>
      <tbody>${c.inventory.map((i) => `<tr><td>${esc(i.data)}</td><td class="small">${esc(i.location)}</td><td>${i.personal ? badge('yes — stays on the device / in memory', 'warn') : badge('no', 'ok')}</td><td class="small">${esc(i.retention)}</td><td class="num">${i.rows ? fmtInt(i.rows) : '—'}</td></tr>`).join('')}</tbody></table>
    </section>
    <section class="card">
      <h2>Privacy regimes <span class="muted small">what each one asks of advertising, and how this system answers</span></h2>
      <div class="regimes" id="regimes">${c.regimes
        .map(
          (r) => `<details class="regime ${attr(r.status)}" data-code="${attr(r.code)}"><summary><strong>${esc(r.code)}</strong> ${esc(r.name)} <span class="muted small">· ${esc(r.jurisdiction)}</span> ${badge(r.status === 'ok' ? 'ok' : 'attention', r.status === 'ok' ? 'ok' : 'warn')}</summary>
            ${r.attention.length ? `<p class="callout warn small">Needs attention: ${esc(r.attention.join('; '))}</p>` : ''}
            <div class="grid2"><div><h4>Obligations</h4><ul>${r.obligations.map((o) => `<li>${esc(o)}</li>`).join('')}</ul></div><div><h4>How this system meets them</h4><ul>${r.how_met.map((o) => `<li>${esc(o)}</li>`).join('')}</ul></div></div></details>`,
        )
        .join('')}</div>
    </section>`;
}

// ---------------------------------------------------------------------------- audit

function auditView(entries) {
  const rows = entries.filter((e) => e.action.startsWith('ad.'));
  return `
    <div class="toolbar"><h1>Audit log <span class="muted small">advertising actions</span></h1></div>
    <section class="card"><table class="table" id="audit"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th></tr></thead>
    <tbody>${rows.map((e) => `<tr><td class="small">${esc(date(e.at))}</td><td class="small">${esc(e.actor_email ?? 'system')}</td><td>${badge(e.action, 'kind')}</td><td class="small mono">${esc(JSON.stringify(e.detail))}</td></tr>`).join('')}</tbody></table>
    ${rows.length ? '' : '<p class="muted">No advertising actions recorded yet.</p>'}</section>`;
}

// ---------------------------------------------------------------------------- controller

async function show() {
  if (!session.token) return showLogin();
  if (!state.me) {
    try {
      state.me = await api.me();
      if (state.me.role !== 'admin') throw new ApiError('This dashboard needs an administrator account.', 403);
    } catch (e) {
      session.clear();
      return showLogin(e.message);
    }
  }
  const name = route();
  render(shell('<p class="muted loading">Loading…</p>', name));
  try {
    let content = '';
    if (name === 'overview') content = overviewView(await api.analytics(state.days, state.kind));
    else if (name === 'payments') content = await loadPayments();
    else if (name === 'campaigns') {
      const list = await api.campaigns(state.includeArchived);
      let detail = null;
      if (state.selected) {
        const [campaign, stats] = await Promise.all([api.campaign(state.selected), api.campaignStats(state.selected, state.days)]);
        detail = { campaign, stats };
      }
      content = campaignsView(list, detail);
    } else if (name === 'sources') content = sourcesView(await api.sources());
    else if (name === 'privacy') content = privacyView(await api.compliance());
    else if (name === 'audit') content = auditView(await api.audit(300));
    render(shell(content, name));
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return showLogin('Your session expired. Sign in again.');
    render(shell(errorBox(e), name));
  }
}

async function act(fn, okMessage) {
  try {
    await fn();
    if (okMessage) toast(okMessage);
    await show();
  } catch (e) {
    toast(e.message, true);
  }
}

document.addEventListener('click', async (ev) => {
  const el = ev.target.closest('[data-action]');
  if (!el) return;
  const { action, id } = el.dataset;
  switch (action) {
    case 'logout':
      await api.logout();
      state.me = null;
      return showLogin();
    case 'csv': {
      const days = state.days;
      return act(async () => {
        const text = await api.analyticsCsv(days);
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
        a.download = `ad-performance-${days}d.csv`;
        a.click();
        URL.revokeObjectURL(a.href);
      }, 'CSV exported');
    }
    case 'pay-csv':
      return act(async () => {
        const text = await api.paymentTransactionsCsv(payQuery());
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
        a.download = `payments-${state.pay.since}-${state.pay.until}.csv`;
        a.click();
        URL.revokeObjectURL(a.href);
      }, 'CSV exported');
    case 'tx':
      state.pay.open = state.pay.open === Number(id) ? null : Number(id);
      await show();
      document.getElementById('tx-detail')?.scrollIntoView({ block: 'nearest' });
      return undefined;
    case 'tx-close':
      state.pay.open = null;
      return show();
    case 'pay-page':
      state.pay.offset = Number(el.dataset.offset) || 0;
      return show();
    case 'pay-search':
      state.pay.q = el.dataset.q || '';
      state.pay.offset = 0;
      return show();
    case 'new':
      state.editing = { ...EMPTY };
      return show();
    case 'cancel-edit':
      state.editing = null;
      return show();
    case 'edit':
      return act(async () => {
        state.editing = await api.campaign(id);
      });
    case 'stats':
      state.selected = state.selected === id ? null : id;
      return show();
    case 'pause':
      return act(() => api.updateCampaign(id, { status: 'paused' }), 'Campaign paused');
    case 'activate':
      return act(() => api.updateCampaign(id, { status: 'active' }), 'Campaign activated');
    case 'archive':
      if (!confirm('Archive this campaign? It is never served again; its totals stay.')) return;
      return act(() => api.archiveCampaign(id), 'Campaign archived');
    default:
      return undefined;
  }
});

document.addEventListener('change', async (ev) => {
  const el = ev.target.closest('[data-action]');
  if (!el) return;
  const { action } = el.dataset;
  if (action === 'pay-preset') {
    // A preset fills the dates at once; "custom" unlocks the date inputs (applied with the form).
    const range = presetRange(el.value);
    state.pay.preset = el.value;
    if (range) Object.assign(state.pay, range);
    state.pay.offset = 0;
    if (range) return show();
    document.getElementById('pay-since')?.removeAttribute('disabled');
    document.getElementById('pay-until')?.removeAttribute('disabled');
    return undefined;
  }
  if (action === 'days') {
    state.days = Number(el.value) || 30;
    return show();
  }
  if (action === 'kind') {
    state.kind = el.value;
    return show();
  }
  if (action === 'archived') {
    state.includeArchived = el.checked;
    return show();
  }
  if (action === 'source') {
    return act(() => api.setSource(el.value), `Ad source: ${kindLabel(el.value)}`);
  }
  return undefined;
});

document.addEventListener('submit', async (ev) => {
  if (ev.target.id === 'pay-filters') {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const v = (k) => String(f.get(k) ?? '').trim();
    const p = state.pay;
    p.preset = v('preset') || 'custom';
    const range = presetRange(p.preset);
    if (range) Object.assign(p, range);
    else {
      p.since = v('since') || p.since;
      p.until = v('until') || p.until;
      if (p.since > p.until) [p.since, p.until] = [p.until, p.since];
    }
    p.method = v('method');
    p.status = v('status');
    p.offset = 0;
    return show();
  }
  if (ev.target.id === 'pay-search-form') {
    ev.preventDefault();
    state.pay.q = String(new FormData(ev.target).get('q') ?? '').trim();
    state.pay.offset = 0;
    return show();
  }
  if (ev.target.id !== 'campaign-form') return;
  ev.preventDefault();
  const body = formToBody(ev.target);
  const id = state.editing?.id;
  await act(async () => {
    const saved = id ? await api.updateCampaign(id, body) : await api.createCampaign(body);
    state.editing = null;
    state.selected = saved.id;
  }, id ? 'Campaign updated' : 'Campaign created');
});

window.addEventListener('hashchange', () => {
  state.editing = null;
  void show();
});

void show();
