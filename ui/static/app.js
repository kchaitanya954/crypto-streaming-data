/* Crypto Signal Dashboard */

// ── Auth state ─────────────────────────────────────────────────────────────────

let authToken    = localStorage.getItem('auth_token') || null;
let authUsername = localStorage.getItem('auth_username') || null;
let authIsAdmin  = localStorage.getItem('auth_is_admin') === 'true';

function authHeaders() {
  return authToken ? { 'Authorization': `Bearer ${authToken}` } : {};
}

/**
 * Authenticated fetch wrapper. On 401, clears token and shows login modal.
 * All API calls must go through this instead of raw fetch().
 */
function apiFetch(url, opts = {}) {
  opts.headers = { ...authHeaders(), ...(opts.headers || {}) };
  return fetch(url, opts).then(r => {
    if (r.status === 401) {
      authToken = null;
      localStorage.removeItem('auth_token');
      localStorage.removeItem('auth_username');
      showAuthModal();
    }
    return r;
  });
}

// ── Currency (INR ↔ USDT) ──────────────────────────────────────────────────────

let displayCurrency = localStorage.getItem('displayCurrency') || 'INR';  // 'INR' or 'USDT'
let usdToInr        = 83.5;   // default fallback; refreshed on load from /api/currency/inr-rate

async function fetchInrRate() {
  try {
    const r = await fetch('/api/currency/inr-rate');
    if (!r.ok) return;
    const d = await r.json();
    if (d.usd_to_inr) usdToInr = d.usd_to_inr;
  } catch (_) {}
}

/** Convert a USDT/USD price to the display currency. */
function toDisplay(usdtValue) {
  return displayCurrency === 'INR' ? usdtValue * usdToInr : usdtValue;
}

/** Convert a display-currency amount back to USDT. */
function fromDisplay(displayValue) {
  return displayCurrency === 'INR' ? displayValue / usdToInr : displayValue;
}

/** Format a display-currency amount with symbol. */
function fmtDisplay(usdtValue, decimals = 2) {
  const v = toDisplay(usdtValue);
  return displayCurrency === 'INR'
    ? '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: decimals })
    : '$' + v.toFixed(decimals);
}

/** Update all currency-sensitive elements after a toggle. */
function applyCurrencyDisplay() {
  const btn = document.getElementById('currency-toggle-btn');
  if (!btn) return;
  if (displayCurrency === 'INR') {
    btn.textContent  = '₹ INR';
    btn.style.color  = '#FF9800';
  } else {
    btn.textContent  = '$ USDT';
    btn.style.color  = '#26A69A';
  }
  updateTrigAmountConversion();
}

/** Show conversion hint next to the amount field. */
function updateTrigAmountConversion() {
  const convEl = document.getElementById('trig-amount-conv');
  if (!convEl) return;
  convEl.textContent = '';   // trigger amount is always USDT — no conversion needed
}

// Currency toggle button
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('currency-toggle-btn');
  if (btn) {
    btn.addEventListener('click', () => {
      displayCurrency = displayCurrency === 'INR' ? 'USDT' : 'INR';
      localStorage.setItem('displayCurrency', displayCurrency);
      applyCurrencyDisplay();
    });
  }
  const amtInp = document.getElementById('trig-amount');
  if (amtInp) amtInp.addEventListener('input', updateTrigAmountConversion);
});

// Fetch rate immediately (non-blocking)
fetchInrRate().then(applyCurrencyDisplay);

// ── State ─────────────────────────────────────────────────────────────────────

let ws               = null;
let currentSymbol    = 'BTCUSDT';
let currentInterval  = '1m';
let markers          = [];
let currentEditId    = null;   // null = create mode, number = edit existing trigger

// Sensitivity controls — persisted in localStorage
// Defaults are overwritten by tier on first interval selection; stored values are manual overrides
let currentAdxMin  = parseFloat(localStorage.getItem('adxMin')  ?? '12');
let currentMinConf = parseInt(  localStorage.getItem('minConf') ?? '1', 10);
let currentCooldown= parseInt(  localStorage.getItem('cooldown') ?? '2', 10);

// Signal filter + selection state
let sigConfFilter = '';     // '' = ALL, 'HIGH', 'MEDIUM', 'LOW'
let sigIvFilter   = '';     // '' = ALL, e.g. '1s', '1m', '15m'
let sigTrigFilter = null;   // null = ALL, {sym, iv} = specific trigger
let selectedCards = new Set();
const knownIntervals = new Set();   // tracks intervals seen so far

// ── Chart creation ────────────────────────────────────────────────────────────

const BASE = {
  layout:     { background: { color: '#131722' }, textColor: '#787B86' },
  grid:       { vertLines: { color: '#1E222D' }, horzLines: { color: '#1E222D' } },
  rightPriceScale: { borderColor: '#2A2E39' },
  crosshair:  { mode: LightweightCharts.CrosshairMode.Normal },
  autoSize:   true,
};

const mainChart = LightweightCharts.createChart(document.getElementById('chart-main'), {
  ...BASE,
  timeScale: {
    borderColor: '#2A2E39',
    timeVisible: true,
    secondsVisible: true,
    rightOffset: 10,        // always leave 10 bars of space on the right
    barSpacing: 8,
  },
});

const macdChart = LightweightCharts.createChart(document.getElementById('chart-macd'), {
  ...BASE,
  timeScale: { borderColor: '#2A2E39', timeVisible: true, visible: false },
});

const rsiChart = LightweightCharts.createChart(document.getElementById('chart-rsi'), {
  ...BASE,
  timeScale: { borderColor: '#2A2E39', timeVisible: true, secondsVisible: true },
  leftPriceScale:  { borderColor: '#2A2E39', visible: true, scaleMargins: { top: 0.1, bottom: 0.1 } },
  rightPriceScale: { borderColor: '#2A2E39', visible: true, scaleMargins: { top: 0.1, bottom: 0.1 } },
});

// ── Series (recreated on each new connection) ─────────────────────────────────

let candleSeries, ema50Series, ema200Series;
let bbUpperSeries, bbMiddleSeries, bbLowerSeries;
let macdHistSeries, macdLineSeries, macdSignalSeries;
let rsiSeries, adxSeries;

function buildSeries() {
  // Main chart — candlestick
  candleSeries = mainChart.addCandlestickSeries({
    upColor: '#26A69A', downColor: '#EF5350',
    borderUpColor: '#26A69A', borderDownColor: '#EF5350',
    wickUpColor:   '#26A69A', wickDownColor:   '#EF5350',
  });

  ema50Series = mainChart.addLineSeries({
    color: '#FF9800', lineWidth: 1,
    title: 'EMA50',
    priceLineVisible: false, crosshairMarkerVisible: false,
    lastValueVisible: true,
  });

  ema200Series = mainChart.addLineSeries({
    color: '#2196F3', lineWidth: 1,
    title: 'EMA200',
    priceLineVisible: false, crosshairMarkerVisible: false,
    lastValueVisible: true,
  });

  // Bollinger Bands on main chart (dashed upper/lower, faint middle)
  bbUpperSeries = mainChart.addLineSeries({
    color: 'rgba(239,83,80,0.45)', lineWidth: 1,
    title: 'BB Upper',
    lineStyle: LightweightCharts.LineStyle.Dashed,
    priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: false,
  });

  bbMiddleSeries = mainChart.addLineSeries({
    color: 'rgba(180,180,180,0.25)', lineWidth: 1,
    title: 'BB Mid',
    priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: false,
  });

  bbLowerSeries = mainChart.addLineSeries({
    color: 'rgba(38,166,154,0.45)', lineWidth: 1,
    title: 'BB Lower',
    lineStyle: LightweightCharts.LineStyle.Dashed,
    priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: false,
  });

  // MACD chart
  macdHistSeries = macdChart.addHistogramSeries({
    priceLineVisible: false, lastValueVisible: false,
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
  });

  macdLineSeries = macdChart.addLineSeries({
    color: '#2196F3', lineWidth: 1, title: 'MACD',
    priceLineVisible: false,
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
  });

  macdSignalSeries = macdChart.addLineSeries({
    color: '#FF9800', lineWidth: 1, title: 'Signal',
    priceLineVisible: false,
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
  });

  macdLineSeries.createPriceLine({
    price: 0, color: '#2A2E39', lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: false,
  });

  // RSI — right price scale, locked 0-100
  rsiSeries = rsiChart.addLineSeries({
    color: '#CE93D8', lineWidth: 1, title: 'RSI',
    priceScaleId: 'right',
    priceLineVisible: false,
    priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
    autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
  });

  rsiSeries.createPriceLine({ price: 70, color: '#EF5350', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true });
  rsiSeries.createPriceLine({ price: 50, color: '#2A2E39', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: false });
  rsiSeries.createPriceLine({ price: 30, color: '#26A69A', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true });

  // ADX — left price scale, independent range
  adxSeries = rsiChart.addLineSeries({
    color: '#FF9800', lineWidth: 1, title: 'ADX',
    priceScaleId: 'left',
    priceLineVisible: false,
    priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
  });

  adxSeries.createPriceLine({
    price: 30, color: '#FF9800', lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
  });
}

function clearSeries() {
  const pairs = [
    [mainChart, [candleSeries, ema50Series, ema200Series, bbUpperSeries, bbMiddleSeries, bbLowerSeries]],
    [macdChart, [macdHistSeries, macdLineSeries, macdSignalSeries]],
    [rsiChart,  [rsiSeries, adxSeries]],
  ];
  pairs.forEach(([chart, series]) =>
    series.forEach(s => { if (s) try { chart.removeSeries(s); } catch (_) {} })
  );
  candleSeries = ema50Series = ema200Series = null;
  bbUpperSeries = bbMiddleSeries = bbLowerSeries = null;
  macdHistSeries = macdLineSeries = macdSignalSeries = null;
  rsiSeries = adxSeries = null;
}

buildSeries();

// ── Time-scale sync (logical range keeps all 3 charts in lock-step) ───────────

let syncing = false;

function syncFrom(src, ...targets) {
  src.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (syncing || !range) return;
    syncing = true;
    targets.forEach(c => c.timeScale().setVisibleLogicalRange(range));
    syncing = false;
  });
}

syncFrom(mainChart, macdChart, rsiChart);
syncFrom(macdChart, mainChart, rsiChart);
syncFrom(rsiChart,  mainChart, macdChart);

// ── Resize handles ────────────────────────────────────────────────────────────

(function initResize() {
  const handles = [
    { el: document.getElementById('rh-1'), prev: document.getElementById('wrap-main'), next: document.getElementById('wrap-macd') },
    { el: document.getElementById('rh-2'), prev: document.getElementById('wrap-macd'), next: document.getElementById('wrap-rsi')  },
  ];

  // Convert flex wrappers to explicit pixel heights once, so drag arithmetic is exact
  function toPx() {
    document.querySelectorAll('.chart-wrapper').forEach(w => {
      if (w.style.flex !== 'none') {
        w.style.height = w.offsetHeight + 'px';
        w.style.flex   = 'none';
      }
    });
  }

  handles.forEach(({ el, prev, next }) => {
    el.addEventListener('mousedown', e => {
      e.preventDefault();
      toPx();
      el.classList.add('dragging');

      const startY  = e.clientY;
      const prevH0  = prev.offsetHeight;
      const nextH0  = next.offsetHeight;
      const MIN     = 60;

      function onMove(ev) {
        const dy      = ev.clientY - startY;
        const newPrev = Math.max(MIN, prevH0 + dy);
        const newNext = Math.max(MIN, nextH0 - dy);
        // Clamp: don't let the two panels steal from each other beyond their minimums
        if (prevH0 + dy >= MIN && nextH0 - dy >= MIN) {
          prev.style.height = newPrev + 'px';
          next.style.height = newNext + 'px';
        }
      }

      function onUp() {
        el.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup',   onUp);
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup',   onUp);
    });
  });
}());

// ── Message handlers ──────────────────────────────────────────────────────────

function onMeta(msg) {
  currentSymbol   = msg.symbol.toUpperCase();
  currentInterval = msg.interval;
  updateLabel();
}

function onCandle(msg) {
  candleSeries.update({ time: msg.time, open: msg.open, high: msg.high, low: msg.low, close: msg.close });

  if (msg.ema50  != null) ema50Series.update( { time: msg.time, value: msg.ema50  });
  if (msg.ema200 != null) ema200Series.update({ time: msg.time, value: msg.ema200 });

  if (msg.bb_upper  != null) bbUpperSeries.update( { time: msg.time, value: msg.bb_upper  });
  if (msg.bb_middle != null) bbMiddleSeries.update({ time: msg.time, value: msg.bb_middle });
  if (msg.bb_lower  != null) bbLowerSeries.update( { time: msg.time, value: msg.bb_lower  });

  if (msg.macd_line != null && msg.macd_signal != null) {
    macdLineSeries.update(  { time: msg.time, value: msg.macd_line   });
    macdSignalSeries.update({ time: msg.time, value: msg.macd_signal });
    macdHistSeries.update({
      time:  msg.time,
      value: msg.macd_hist,
      color: msg.macd_hist >= 0 ? 'rgba(38,166,154,0.65)' : 'rgba(239,83,80,0.65)',
    });
  }

  if (msg.rsi_val != null) rsiSeries.update({ time: msg.time, value: msg.rsi_val });
  if (msg.adx_val != null) adxSeries.update({ time: msg.time, value: msg.adx_val });
}

function onLive(msg) {
  candleSeries.update({ time: msg.time, open: msg.open, high: msg.high, low: msg.low, close: msg.close });
}

function onReady() {
  [mainChart, macdChart, rsiChart].forEach(c => c.timeScale().scrollToRealTime());
  loadHistoricalSignals();
}

function onSignal(msg) {
  markers.push({
    time:     msg.time,
    position: msg.direction === 'BUY' ? 'belowBar' : 'aboveBar',
    color:    msg.direction === 'BUY' ? '#26A69A'  : '#EF5350',
    shape:    msg.direction === 'BUY' ? 'arrowUp'  : 'arrowDown',
    text:     `${msg.direction} (${msg.confidence})`,
    size:     1,
  });
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);

  // Always show signal card; highlight trigger-matched ones
  addSignalCard(msg, msg.trigger_matched);
  if (msg.trigger_matched) pushNotification(msg);
  mainChart.timeScale().scrollToRealTime();
  if (analyticsOpen) loadAnalytics();
}

// ── Historical signal pre-load ────────────────────────────────────────────────

function loadHistoricalSignals() {
  // Load all recent signals across all symbols and intervals
  apiFetch('/api/signals/history?limit=150')
    .then(r => r.json())
    .then(signals => {
      if (!Array.isArray(signals) || signals.length === 0) return;
      // Newest-first from API; prepend so newest is on top
      signals.forEach(s => {
        addSignalCard({
          id:            s.id,
          symbol:        s.symbol,
          interval:      s.interval,
          direction:     s.direction,
          confidence:    s.confidence,
          entry_price:   s.entry_price,
          time:          s.open_time / 1000,
          reasons:       Array.isArray(s.reasons) ? s.reasons : [],
          trend_note:    s.trend_note || '',
          macd_val:      s.macd_val,
          adx_val:       s.adx_val,
          trigger_names: [],
        });
      });
    })
    .catch(() => {});
}

// ── Portfolio panel ───────────────────────────────────────────────────────────

function fetchPortfolio() {
  const updEl = document.getElementById('pf-updated');
  if (updEl) updEl.textContent = 'Loading…';
  apiFetch('/api/portfolio')
    .then(r => r.json())
    .then(data => {
      const list = document.getElementById('portfolio-list');
      if (data.error || data.detail) {
        // Not configured — show message instead of polling
        list.innerHTML = `<div class="pf-empty" style="color:#787B86;font-size:11px">${data.detail || data.error}</div>`;
        if (updEl) updEl.textContent = '';
        return;
      }
      if (!Array.isArray(data)) return;
      list.innerHTML = '';
      if (data.length === 0) {
        list.innerHTML = '<div class="pf-empty">No balances</div>';
        return;
      }
      data.forEach(b => {
        const avail  = parseFloat(b.balance        || 0);
        const locked = parseFloat(b.locked_balance || 0);
        const total  = avail + locked;
        if (total < 0.000001) return;
        const row = document.createElement('div');
        row.className = 'pf-row';
        row.innerHTML =
          `<span class="pf-currency">${b.currency}</span>` +
          `<span class="pf-amount">${total.toFixed(6)}</span>` +
          (locked > 0 ? `<span class="pf-locked">${locked.toFixed(6)} locked</span>` : '');
        list.appendChild(row);
      });
      if (updEl) updEl.textContent =
        'Updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    })
    .catch(() => {
      if (updEl) updEl.textContent = 'Unavailable';
    });
}

// Load portfolio once on dashboard init — no auto-poll (uses authenticated user's own API keys)
// Use the refresh button in the UI to reload manually.

// ── Signal card + filter + manage ────────────────────────────────────────────

function getCardTriggers(reasons) {
  const cats = [];
  if (reasons.some(r => r.startsWith('RSI')))   cats.push('RSI');
  if (reasons.some(r => r.startsWith('Stoch'))) cats.push('Stoch');
  if (reasons.some(r => r.startsWith('OBV')))   cats.push('OBV');
  return cats;
}

function cardVisible(card) {
  const confOk = !sigConfFilter || card.dataset.conf     === sigConfFilter;
  const ivOk   = !sigIvFilter   || card.dataset.interval === sigIvFilter;
  let trigOk = true;
  if (sigTrigFilter) {
    trigOk = card.dataset.symbol   === sigTrigFilter.sym
          && card.dataset.interval === sigTrigFilter.iv;
  }
  return confOk && ivOk && trigOk;
}

function applyCardFilter(card) {
  card.style.display = cardVisible(card) ? '' : 'none';
}

function applyAllFilters() {
  document.querySelectorAll('#signal-list .signal-card').forEach(applyCardFilter);
}

function toggleSelectCard(card) {
  if (selectedCards.has(card)) {
    selectedCards.delete(card);
    card.classList.remove('sc-selected');
  } else {
    selectedCards.add(card);
    card.classList.add('sc-selected');
  }
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('sig-bulk-bar');
  const n   = selectedCards.size;
  bar.style.display = n > 0 ? 'flex' : 'none';
  document.getElementById('sig-bulk-info').textContent = `${n} selected`;
}

function checkEmptyList() {
  const list = document.getElementById('signal-list');
  if (!list.querySelector('.signal-card')) {
    list.innerHTML = '<div class="no-sig">No signals</div>';
  }
}

function closeDeleteMenus() {
  document.querySelectorAll('.sc-del-menu').forEach(m => { m.hidden = true; });
}

function removeSignalCard(card) {
  const t = parseFloat(card.dataset.time);
  markers = markers.filter(m => m.time !== t);
  if (candleSeries) candleSeries.setMarkers(markers);
  selectedCards.delete(card);
  card.remove();
  updateBulkBar();
  checkEmptyList();
  if (card.dataset.id) {
    apiFetch(`/api/signals/${card.dataset.id}`, { method: 'DELETE' }).catch(() => {});
  }
}

function deleteByTrigger(trigger) {
  closeDeleteMenus();
  document.querySelectorAll('#signal-list .signal-card').forEach(card => {
    if ((card.dataset.triggers || '').split(',').includes(trigger)) {
      markers = markers.filter(m => m.time !== parseFloat(card.dataset.time));
      selectedCards.delete(card);
      if (card.dataset.id) {
        apiFetch(`/api/signals/${card.dataset.id}`, { method: 'DELETE' }).catch(() => {});
      }
      card.remove();
    }
  });
  if (candleSeries) candleSeries.setMarkers(markers);
  updateBulkBar();
  checkEmptyList();
}

function deleteAllSignals() {
  closeDeleteMenus();
  markers = [];
  if (candleSeries) candleSeries.setMarkers([]);
  selectedCards.clear();
  document.getElementById('signal-list').innerHTML = '<div class="no-sig">No signals</div>';
  updateBulkBar();
  // Delete ALL signals from DB (no filter — wipes entire signals table)
  apiFetch('/api/signals', { method: 'DELETE' }).catch(() => {});
}

function deleteSelected() {
  [...selectedCards].forEach(card => {
    markers = markers.filter(m => m.time !== parseFloat(card.dataset.time));
    if (card.dataset.id) {
      apiFetch(`/api/signals/${card.dataset.id}`, { method: 'DELETE' }).catch(() => {});
    }
    card.remove();
  });
  if (candleSeries) candleSeries.setMarkers(markers);
  selectedCards.clear();
  updateBulkBar();
  checkEmptyList();
}

document.addEventListener('click', closeDeleteMenus);

function addSignalCard(msg, triggerMatch = false) {
  const list    = document.getElementById('signal-list');
  list.querySelector('.no-sig')?.remove();

  const time    = new Date(msg.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const priceUsdt = msg.entry_price;
  const price   = displayCurrency === 'INR'
    ? '₹' + Math.round(priceUsdt * usdToInr).toLocaleString('en-IN')
    : '$' + priceUsdt.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const reasons = msg.reasons || [];
  const triggers = getCardTriggers(reasons);

  const trigItems = triggers.map(t =>
    `<div class="sc-del-item del-trigger" data-action="trigger" data-trig="${t}">Delete all ${t} signals</div>`
  ).join('');

  const card = document.createElement('div');
  card.className        = `signal-card ${msg.direction}${triggerMatch ? ' trigger-match' : ''}`;
  card.dataset.conf     = msg.confidence;
  card.dataset.triggers = triggers.join(',');
  card.dataset.time     = msg.time;
  if (msg.id       != null) card.dataset.id            = msg.id;
  if (msg.symbol)           card.dataset.symbol        = msg.symbol;
  if (msg.interval)         card.dataset.interval      = msg.interval;
  if (msg.trigger_names)    card.dataset.triggerNames  = msg.trigger_names.join(',');
  if (msg.interval)         registerIntervalFilter(msg.interval);

  const symLabel = msg.symbol
    ? `<span class="sc-sym">${msg.symbol}${msg.interval ? ' · ' + msg.interval : ''}</span>`
    : '';

  card.innerHTML = `
    <button class="sc-del-btn" title="Delete options">✕</button>
    <div class="sc-del-menu" hidden>
      <div class="sc-del-item" data-action="one">Delete this signal</div>
      ${trigItems}
      <div class="sc-del-item del-all" data-action="all">Delete all signals</div>
    </div>
    <div class="sc-row">
      <span class="sc-dir">${msg.direction}</span>
      <span class="sc-conf-${msg.confidence}">${msg.confidence}</span>
      ${symLabel}
    </div>
    <div class="sc-price">$${price}</div>
    <div class="sc-time">${time}</div>
    ${reasons.length ? `<div class="sc-reason">${reasons.join(' · ')}</div>` : ''}
    ${msg.trend_note ? `<div class="sc-trend">${msg.trend_note}</div>` : ''}
    ${msg.rec_buy_pct != null ? `<div class="sc-rec">Rec: ${msg.rec_buy_pct}%</div>` : ''}
  `;

  card.querySelector('.sc-del-btn').addEventListener('click', e => {
    e.stopPropagation();
    const menu    = card.querySelector('.sc-del-menu');
    const wasOpen = !menu.hidden;
    closeDeleteMenus();
    menu.hidden = wasOpen;
  });

  card.querySelector('.sc-del-menu').addEventListener('click', e => {
    e.stopPropagation();
    const item = e.target.closest('.sc-del-item');
    if (!item) return;
    if      (item.dataset.action === 'one')     removeSignalCard(card);
    else if (item.dataset.action === 'trigger') deleteByTrigger(item.dataset.trig);
    else if (item.dataset.action === 'all')     deleteAllSignals();
  });

  card.addEventListener('click', e => {
    if (e.target.closest('.sc-del-btn, .sc-del-menu')) return;
    toggleSelectCard(card);
  });

  applyCardFilter(card);
  list.prepend(card);
}

// ── Browser notification ──────────────────────────────────────────────────────

function pushNotification(msg) {
  if (Notification.permission !== 'granted') return;
  const icon = msg.direction === 'BUY' ? '🟢' : '🔴';
  new Notification(`${icon} ${msg.direction} (${msg.confidence}) · ${currentSymbol} ${currentInterval}`, {
    body: `${fmtDisplay(msg.entry_price)}  ·  ${msg.reasons.join(', ')}`,
  });
}

// ── Status + label ────────────────────────────────────────────────────────────

function setStatus(connected) {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  dot.className    = connected ? 'ok' : '';
  text.textContent = connected ? 'Live' : 'Reconnecting…';
  text.style.color = connected ? '#26A69A' : '#EF5350';
}

function updateLabel() {
  document.getElementById('label-main').innerHTML =
    `${currentSymbol} ${currentInterval}&nbsp;·&nbsp;` +
    `EMA50 <span style="color:#FF9800">─</span>&nbsp;` +
    `EMA200 <span style="color:#2196F3">─</span>&nbsp;·&nbsp;` +
    `BB(20) <span style="color:rgba(239,83,80,0.8)">- -</span>`;
  document.title = `${currentSymbol} ${currentInterval} · Signals`;
}

// ── WebSocket connect / reconnect ─────────────────────────────────────────────

function connect(symbol, interval) {
  if (ws) {
    ws.onclose = null;
    ws.close();
  }

  markers = [];
  clearSeries();
  buildSeries();
  document.getElementById('signal-list').innerHTML = '<div class="no-sig">Loading…</div>';
  setStatus(false);

  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(
    `${wsProto}//${location.host}/ws?symbol=${symbol.toLowerCase()}&interval=${interval}` +
    `&adx_min=${currentAdxMin}&min_conf=${currentMinConf}&cooldown=${currentCooldown}`
  );

  ws.onopen = () => {
    setStatus(true);
    Notification.requestPermission();
  };

  ws.onclose = () => {
    setStatus(false);
    setTimeout(() => connect(symbol, interval), 3000);
  };

  ws.onerror = () => setStatus(false);

  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if      (msg.type === 'meta')   onMeta(msg);
    else if (msg.type === 'candle') onCandle(msg);
    else if (msg.type === 'live')   onLive(msg);
    else if (msg.type === 'ready')  onReady();
    else if (msg.type === 'signal') onSignal(msg);
  };
}

// ── Controls ──────────────────────────────────────────────────────────────────

const VALID_INTERVALS = new Set([
  '1s',
  '1m','3m','5m','15m','30m',
  '1h','2h','4h','6h','8h','12h',
  '1d','3d','1w','1M',
]);

function ivTier(iv) {
  const u = iv.slice(-1);
  const n = parseInt(iv) || 1;
  const mins = ({'s':1/60,'m':1,'h':60,'d':1440,'w':10080}[u] || 1) * n;
  if (mins < 3)   return 'scalping';
  if (mins < 60)  return 'intraday';
  if (mins < 360) return 'swing';
  return 'position';
}

const TIER_DEFAULTS = {
  scalping: { adx: 12, cooldown: 2, conf: 1 },
  intraday: { adx: 18, cooldown: 3, conf: 1 },
  swing:    { adx: 20, cooldown: 5, conf: 1 },
  position: { adx: 22, cooldown: 3, conf: 1 },
};

function applyTierToSensitivityBar(iv) {
  const tier = ivTier(iv);
  const d    = TIER_DEFAULTS[tier];
  currentAdxMin   = d.adx;
  currentMinConf  = d.conf;
  currentCooldown = d.cooldown;
  localStorage.setItem('adxMin',   currentAdxMin);
  localStorage.setItem('minConf',  currentMinConf);
  localStorage.setItem('cooldown', currentCooldown);
  updateSensitivityDisplay();
}

function updateSensitivityDisplay() {
  const adxEl = document.getElementById('adx-val-display');
  const cdEl  = document.getElementById('cd-val-display');
  const badge = document.getElementById('tier-badge');
  if (adxEl) adxEl.textContent = currentAdxMin;
  if (cdEl)  cdEl.textContent  = currentCooldown;
  if (badge) badge.textContent = ivTier(currentInterval);
  // Highlight active confidence button
  document.querySelectorAll('.conf-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.conf, 10) === currentMinConf);
  });
}

function applyTierToTriggerForm(iv) {
  const d = TIER_DEFAULTS[ivTier(iv)];
  const hint = document.getElementById('trig-tier-hint');
  if (hint) hint.textContent = `tier: ${ivTier(iv)} · adx=${d.adx} cd=${d.cooldown}`;
  // Set placeholders to show tier defaults; leave value blank (user can override)
  const adxEl = document.getElementById('trig-adx');
  const cdEl  = document.getElementById('trig-cd');
  if (adxEl) adxEl.placeholder = d.adx;
  if (cdEl)  cdEl.placeholder  = d.cooldown;
}

document.getElementById('iv-select').addEventListener('change', e => {
  currentInterval = e.target.value;
  applyTierToSensitivityBar(currentInterval);
  connect(currentSymbol, currentInterval);
});

// ── Sensitivity controls ──────────────────────────────────────────────────────

function activateBtn(selector, activeBtn) {
  document.querySelectorAll(selector).forEach(b => b.classList.remove('active'));
  activeBtn.classList.add('active');
}

// Initialise display from stored/default state
updateSensitivityDisplay();

document.querySelectorAll('.adx-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const delta = parseFloat(btn.dataset.adx);   // e.g. -5 or +5
    currentAdxMin = Math.max(0, currentAdxMin + delta);
    localStorage.setItem('adxMin', currentAdxMin);
    updateSensitivityDisplay();
    connect(currentSymbol, currentInterval);
  });
});

document.querySelectorAll('.conf-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    currentMinConf = parseInt(btn.dataset.conf, 10);
    localStorage.setItem('minConf', currentMinConf);
    updateSensitivityDisplay();
    connect(currentSymbol, currentInterval);
  });
});

document.querySelectorAll('.cd-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const delta = parseInt(btn.dataset.cd, 10);  // e.g. -1 or +1
    currentCooldown = Math.max(0, currentCooldown + delta);
    localStorage.setItem('cooldown', currentCooldown);
    updateSensitivityDisplay();
    connect(currentSymbol, currentInterval);
  });
});

document.getElementById('symbol-input').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  const val = e.target.value.trim().toUpperCase();
  if (val) {
    currentSymbol = val;
    connect(currentSymbol, currentInterval);
  }
});

// ── Trigger multi-select + bulk delete ───────────────────────────────────────

let selectedTriggers = new Set();

function updateTrigBulkBar() {
  const n   = selectedTriggers.size;
  const bar = document.getElementById('trig-bulk-bar');
  bar.style.display = n > 0 ? 'flex' : 'none';
  document.getElementById('trig-bulk-info').textContent = `${n} selected`;
  // Sync select-all checkbox state
  const allCbs = [...document.querySelectorAll('.trig-row-cb')];
  const selAll = document.getElementById('trig-select-all');
  if (allCbs.length === 0) {
    selAll.indeterminate = false; selAll.checked = false;
  } else if (n === 0) {
    selAll.indeterminate = false; selAll.checked = false;
  } else if (n === allCbs.length) {
    selAll.indeterminate = false; selAll.checked = true;
  } else {
    selAll.indeterminate = true;
  }
}

async function deleteSelectedTriggers() {
  const ids = [...selectedTriggers];
  if (ids.length === 0) return;
  try {
    await apiFetch('/api/triggers/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    selectedTriggers.clear();
    loadTriggers();
  } catch (e) { /* ignore */ }
}

document.getElementById('trig-del-sel-btn').addEventListener('click', deleteSelectedTriggers);

document.getElementById('trig-select-all').addEventListener('change', e => {
  const cbs = document.querySelectorAll('.trig-row-cb');
  if (e.target.checked) {
    cbs.forEach(cb => {
      cb.checked = true;
      selectedTriggers.add(parseInt(cb.dataset.id, 10));
    });
  } else {
    cbs.forEach(cb => { cb.checked = false; });
    selectedTriggers.clear();
  }
  updateTrigBulkBar();
});

// ── Signal filter + bulk controls ────────────────────────────────────────────

document.querySelectorAll('.cf-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.cf-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    sigConfFilter = btn.dataset.cf;
    applyAllFilters();
  });
});

// Called whenever a new interval appears in the signal list
function registerIntervalFilter(interval) {
  if (!interval || knownIntervals.has(interval)) return;
  knownIntervals.add(interval);
  const container = document.getElementById('iv-filter-btns');
  const btn = document.createElement('button');
  btn.className    = 'sf-btn ivf-btn';
  btn.textContent  = interval;
  btn.dataset.iv   = interval;
  btn.addEventListener('click', () => {
    container.querySelectorAll('.ivf-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    sigIvFilter = interval;
    applyAllFilters();
  });
  container.appendChild(btn);
}

// Bind the static ALL button for IV filter
document.getElementById('iv-filter-btns').querySelector('.ivf-btn').addEventListener('click', function () {
  document.getElementById('iv-filter-btns').querySelectorAll('.ivf-btn').forEach(b => b.classList.remove('active'));
  this.classList.add('active');
  sigIvFilter = '';
  applyAllFilters();
});

function bindTriggerFilterBtns() {
  const container = document.getElementById('trig-filter-btns');
  container.querySelectorAll('.tf-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const sym = btn.dataset.tfSym;
      const iv  = btn.dataset.tfIv;
      sigTrigFilter = (sym && iv) ? { sym, iv } : null;
      applyAllFilters();
    });
  });
}
bindTriggerFilterBtns(); // bind the initial ALL button

document.getElementById('sig-del-all').addEventListener('click',     () => deleteAllSignals());
document.getElementById('sig-del-sel-btn').addEventListener('click', () => deleteSelected());
document.getElementById('sig-desel-btn').addEventListener('click', () => {
  selectedCards.forEach(c => c.classList.remove('sc-selected'));
  selectedCards.clear();
  updateBulkBar();
});

// ── Triggers panel ────────────────────────────────────────────────────────────

function renderTriggerFilterBtns(list) {
  const container = document.getElementById('trig-filter-btns');
  // Reset to ALL
  container.innerHTML = '<button class="sf-btn tf-btn active" data-tf-sym="" data-tf-iv="">ALL</button>';
  (list || []).filter(t => t.active).forEach(t => {
    const label = t.name || `${t.symbol} ${t.interval}`;
    const btn = document.createElement('button');
    btn.className    = 'sf-btn tf-btn';
    btn.textContent  = label;
    btn.dataset.tfSym = t.symbol;
    btn.dataset.tfIv  = t.interval;
    btn.title = `${t.symbol} ${t.interval} ≥${t.min_confidence}`;
    container.appendChild(btn);
  });
  bindTriggerFilterBtns();
}

function loadTriggers() {
  apiFetch('/api/triggers')
    .then(r => r.json())
    .then(list => {
      renderTriggers(list);
      renderTriggerFilterBtns(list);
    })
    .catch(() => {});
}

function renderTriggers(list) {
  const el = document.getElementById('triggers-list');
  // Clear selection state on every re-render
  selectedTriggers.clear();
  document.getElementById('trig-select-all').checked       = false;
  document.getElementById('trig-select-all').indeterminate = false;
  document.getElementById('trig-bulk-bar').style.display   = 'none';

  if (!Array.isArray(list) || list.length === 0) {
    el.innerHTML = '<div class="trig-empty">No triggers</div>';
    return;
  }
  el.innerHTML = '';
  list.forEach(t => {
    const row = document.createElement('div');
    row.className = `trig-row${t.active ? '' : ' inactive'}`;
    row.dataset.id = t.id;
    const adxHint  = t.adx_threshold != null ? ` adx≥${t.adx_threshold}` : '';
    const cdHint   = t.cooldown_bars  != null ? ` cd${t.cooldown_bars}`   : '';
    const nameLabel = t.name ? `<span style="font-size:10px;color:#D1D4DC;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.name}">${t.name}</span>` : '';
    let amtLabel = '';
    if (t.trade_amount_usdt) {
      const amtDisp = displayCurrency === 'INR'
        ? `₹${Math.round(t.trade_amount_usdt * usdToInr).toLocaleString('en-IN')}`
        : `$${t.trade_amount_usdt.toFixed(2)}`;
      amtLabel = `<span style="font-size:8px;color:#FF9800;font-weight:600">${amtDisp}</span>`;
    }
    row.innerHTML =
      `<input type="checkbox" class="trig-cb trig-row-cb" data-id="${t.id}" />` +
      nameLabel +
      `<span class="trig-sym">${t.symbol}</span>` +
      `<span class="trig-iv">${t.interval}</span>` +
      `<span class="trig-conf trig-conf-${t.min_confidence}">${t.min_confidence}</span>` +
      amtLabel +
      (adxHint || cdHint ? `<span style="font-size:8px;color:#4C525E">${adxHint}${cdHint}</span>` : '') +
      `<span class="trig-edit" title="Edit">✏</span>` +
      `<span class="trig-toggle" title="${t.active ? 'Disable' : 'Enable'}">${t.active ? '✓' : '○'}</span>` +
      `<span class="trig-del" title="Delete">✕</span>`;

    row.querySelector('.trig-row-cb').addEventListener('change', e => {
      const id = parseInt(e.target.dataset.id, 10);
      if (e.target.checked) selectedTriggers.add(id);
      else                   selectedTriggers.delete(id);
      updateTrigBulkBar();
    });

    row.querySelector('.trig-edit').addEventListener('click', () => openEditForm(t));

    row.querySelector('.trig-toggle').addEventListener('click', () => {
      apiFetch(`/api/triggers/${t.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active: !t.active }),
      }).then(r => { if (r.ok) loadTriggers(); });
    });

    row.querySelector('.trig-del').addEventListener('click', () => {
      if (!confirm(`Delete trigger "${t.symbol} ${t.interval}"?`)) return;
      const delSigs = confirm(`Also delete all signals for ${t.symbol} ${t.interval}?\nOK = delete signals too, Cancel = keep signals.`);
      apiFetch(`/api/triggers/${t.id}?delete_signals=${delSigs}`, { method: 'DELETE' })
        .then(r => { if (r.ok) loadTriggers(); });
    });

    el.appendChild(row);
  });
}

// Auto-fill tier hints when interval changes inside the trigger form
document.getElementById('trig-iv').addEventListener('change', e => {
  applyTierToTriggerForm(e.target.value);
});

function openEditForm(t) {
  currentEditId = t.id;
  document.getElementById('trig-name').value = t.name || '';
  document.getElementById('trig-sym').value  = t.symbol;
  for (const opt of document.getElementById('trig-iv').options)
    opt.selected = opt.value === t.interval;
  for (const opt of document.getElementById('trig-conf').options)
    opt.selected = opt.value === t.min_confidence;
  // Populate ADX / cooldown (show stored value or blank for "tier default")
  document.getElementById('trig-adx').value    = t.adx_threshold    != null ? t.adx_threshold    : '';
  document.getElementById('trig-cd').value      = t.cooldown_bars    != null ? t.cooldown_bars    : '';
  // Show stored USDT amount (always USDT)
  document.getElementById('trig-amount').value = t.trade_amount_usdt != null ? t.trade_amount_usdt : '';
  updateTrigAmountConversion();
  applyTierToTriggerForm(t.interval);
  document.getElementById('trig-save-btn').textContent = 'Update Trigger';
  const form = document.getElementById('trig-add-form');
  form.classList.add('open');
  form.scrollIntoView({ block: 'nearest' });
}

// + button: open create form (or switch back from edit mode)
document.getElementById('trig-add-btn').addEventListener('click', () => {
  const form    = document.getElementById('trig-add-form');
  const opening = !form.classList.contains('open') || currentEditId !== null;
  currentEditId = null;
  document.getElementById('trig-save-btn').textContent = 'Save Trigger';
  if (opening) {
    document.getElementById('trig-name').value = '';
    document.getElementById('trig-sym').value  = currentSymbol;
    document.getElementById('trig-adx').value  = '';
    document.getElementById('trig-cd').value   = '';
    for (const opt of document.getElementById('trig-iv').options)
      opt.selected = opt.value === currentInterval;
    applyTierToTriggerForm(currentInterval);
    form.classList.add('open');
    form.scrollIntoView({ block: 'nearest' });
  } else {
    form.classList.remove('open');
  }
});

document.getElementById('trig-save-btn').addEventListener('click', () => {
  const btn    = document.getElementById('trig-save-btn');
  const name   = document.getElementById('trig-name').value.trim();
  const sym    = document.getElementById('trig-sym').value.trim().toUpperCase() || currentSymbol;
  const iv     = document.getElementById('trig-iv').value;
  const conf   = document.getElementById('trig-conf').value;
  const editId = currentEditId;

  // Name is required
  const nameInput = document.getElementById('trig-name');
  if (!name) {
    nameInput.style.borderColor = '#EF5350';
    btn.textContent = '✕ Name is required';
    setTimeout(() => {
      nameInput.style.borderColor = '';
      btn.textContent = editId ? 'Update Trigger' : 'Save Trigger';
    }, 2500);
    return;
  }
  nameInput.style.borderColor = '';

  // Basic symbol validation: 3-12 uppercase letters/digits, must end with USDT/BTC/ETH/BNB/INR
  const symInput = document.getElementById('trig-sym');
  const validSym = /^[A-Z0-9]{3,12}$/.test(sym) && /^.+(USDT|BTC|ETH|BNB|INR)$/.test(sym);
  if (!validSym) {
    symInput.style.borderColor = '#EF5350';
    btn.textContent = '✕ Invalid symbol (e.g. BTCUSDT)';
    setTimeout(() => {
      symInput.style.borderColor = '';
      btn.textContent = editId ? 'Update Trigger' : 'Save Trigger';
    }, 2500);
    return;
  }
  symInput.style.borderColor = '';

  btn.disabled    = true;
  btn.textContent = editId ? 'Updating…' : 'Saving…';

  const url    = editId ? `/api/triggers/${editId}` : '/api/triggers';
  const method = editId ? 'PUT' : 'POST';

  const adxRaw    = document.getElementById('trig-adx').value.trim();
  const cdRaw     = document.getElementById('trig-cd').value.trim();
  const amountRaw = document.getElementById('trig-amount').value.trim();
  const amountDisplay = amountRaw !== '' ? parseFloat(amountRaw) : 0;
  // Amount field is always in USDT — no currency conversion needed
  const amountUsdt = amountDisplay || 0;

  // Amount required on create
  if (!editId && amountUsdt < 1) {
    const amtInp = document.getElementById('trig-amount');
    amtInp.style.borderColor = '#EF5350';
    btn.textContent = '✕ Amount required (min $1 USDT)';
    setTimeout(() => {
      amtInp.style.borderColor = '';
      btn.textContent = 'Save Trigger';
    }, 2500);
    return;
  }

  const errEl = document.getElementById('trig-form-error');
  errEl.style.display = 'none';

  const payload = {
    name, symbol: sym, interval: iv, min_confidence: conf,
    adx_threshold:    adxRaw !== '' ? parseFloat(adxRaw)  : null,
    cooldown_bars:    cdRaw  !== '' ? parseInt(cdRaw, 10)  : null,
    trade_amount_usdt: parseFloat(amountUsdt.toFixed(4)),
  };

  apiFetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  .then(async r => {
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(data.error || data.detail || `HTTP ${r.status}`);
    }
    return r.json();
  })
  .then(() => {
    document.getElementById('trig-add-form').classList.remove('open');
    document.getElementById('trig-name').value   = '';
    document.getElementById('trig-sym').value    = '';
    document.getElementById('trig-amount').value = '';
    btn.disabled    = false;
    btn.textContent = 'Save Trigger';
    currentEditId   = null;
    loadTriggers();
  })
  .catch(err => {
    errEl.textContent   = err.message;
    errEl.style.display = 'block';
    btn.disabled    = false;
    btn.textContent = '✕ Failed – retry';
    setTimeout(() => {
      btn.textContent = editId ? 'Update Trigger' : 'Save Trigger';
    }, 3000);
  });
});

// loadTriggers() and connect() are now called via checkAuth() → initDashboard()

// ── Mobile sidebar toggle ─────────────────────────────────────────────────────

(function initMobileSidebar() {
  const toggle  = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');

  function openSidebar()  { sidebar.classList.add('mobile-open');    overlay.classList.add('visible'); }
  function closeSidebar() { sidebar.classList.remove('mobile-open'); overlay.classList.remove('visible'); }

  toggle.addEventListener('click',  openSidebar);
  overlay.addEventListener('click', closeSidebar);
}());

// ── Analytics ─────────────────────────────────────────────────────────────────

let analyticsOpen   = false;
const knownSymbols  = new Set();   // accumulates all symbols ever seen; never cleared

document.getElementById('analytics-btn').addEventListener('click', () => {
  document.getElementById('analytics-modal').classList.add('open');
  analyticsOpen = true;
  loadAnalytics();
  apiFetch('/api/adaptive').then(r => r.json()).then(renderAdaptiveState).catch(() => {});
});

document.getElementById('an-close').addEventListener('click', closeAnalytics);
document.getElementById('analytics-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('analytics-modal')) closeAnalytics();
});
document.getElementById('an-refresh').addEventListener('click', loadAnalytics);
document.getElementById('an-symbol').addEventListener('change', loadAnalytics);
document.getElementById('an-interval').addEventListener('change', loadAnalytics);
document.getElementById('an-conf').addEventListener('change', loadAnalytics);
document.getElementById('an-period').addEventListener('change', loadAnalytics);

function closeAnalytics() {
  document.getElementById('analytics-modal').classList.remove('open');
  analyticsOpen = false;
}

function pct(v, decimals) {
  // Auto-scale decimals for tiny values so they never display as "+0.00%"
  if (decimals === undefined) {
    const abs = Math.abs(v);
    decimals = abs === 0 ? 2 : abs < 0.001 ? 6 : abs < 0.1 ? 4 : 2;
  }
  return (v >= 0 ? '+' : '') + v.toFixed(decimals) + '%';
}
function pctClass(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu'; }

function simParams() {
  return {
    initial_usdt: parseFloat(document.getElementById('sim-usdt').value)    || 100,
    buy_pct:      parseFloat(document.getElementById('sim-buy-pct').value) || 10,
    sell_pct:     parseFloat(document.getElementById('sim-sell-pct').value)|| 100,
  };
}

document.getElementById('sim-run').addEventListener('click', loadAnalytics);

function loadAnalytics() {
  const sym      = document.getElementById('an-symbol').value;
  const interval = document.getElementById('an-interval').value;
  const conf     = document.getElementById('an-conf').value;
  const period   = document.getElementById('an-period').value;
  const sim      = simParams();
  const params   = new URLSearchParams({
    symbol: sym, interval, confidence: conf, period,
    initial_usdt: sim.initial_usdt, buy_pct: sim.buy_pct, sell_pct: sim.sell_pct,
  });
  apiFetch(`/api/analytics?${params}`)
    .then(r => r.json())
    .then(renderAnalytics)
    .catch(() => {});
  // Load real trade P&L alongside signal analytics
  apiFetch(`/api/analytics/real-trades?period=${period}`)
    .then(r => r.json())
    .then(renderRealTrades)
    .catch(() => {});
}

function renderRealTrades(d) {
  if (!d || d.error) return;
  const fmt2 = n => (n != null ? n.toFixed(2) : '—');
  const fmt4 = n => (n != null ? n.toFixed(4) : '—');

  const noData  = document.getElementById('rt-no-data');
  const summary = document.getElementById('rt-summary');
  const openWrap = document.getElementById('rt-open-wrap');

  // Show open positions even if no completed cycles
  if (d.open_positions && d.open_positions.length > 0) {
    openWrap.style.display = '';
    document.getElementById('rt-open-list').innerHTML = d.open_positions.map(p => `
      <span style="background:#131722;border:1px solid #26A69A33;border-radius:4px;padding:4px 8px;font-size:11px;color:#D1D4DC">
        <span style="color:#26A69A;font-weight:700">${p.symbol}</span>
        ${p.qty_held.toPrecision(5)} coins
        · avg buy <span style="color:#FF9800">$${fmt2(p.avg_buy_price)}</span>
        · invested <span style="color:#EF5350">$${fmt2(p.total_bought_usdt)}</span>
      </span>`).join('');
  } else {
    openWrap.style.display = 'none';
  }

  if (d.completed_cycles === 0) {
    noData.style.display = '';
    summary.style.display = 'none';
    document.getElementById('rt-cycles-body').innerHTML = '';
    return;
  }

  noData.style.display = 'none';
  summary.style.display = '';

  const setCard = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    el.className = 'an-card-val' + (cls ? ' ' + cls : '');
  };

  setCard('rt-v-cycles',   d.completed_cycles);
  setCard('rt-v-winrate',  d.win_rate != null ? d.win_rate + '%' : '—',
                           d.win_rate != null ? pctClass(d.win_rate - 50) : '');
  setCard('rt-v-invested', '$' + fmt2(d.total_invested_usdt));
  setCard('rt-v-returned', '$' + fmt2(d.total_returned_usdt));
  setCard('rt-v-pnl-usdt',
    (d.net_pnl_usdt >= 0 ? '+$' : '-$') + Math.abs(d.net_pnl_usdt).toFixed(2),
    pctClass(d.net_pnl_usdt));
  setCard('rt-v-pnl-pct',
    d.net_pnl_pct != null ? pct(d.net_pnl_pct) : '—',
    d.net_pnl_pct != null ? pctClass(d.net_pnl_pct) : '');

  const tbody = document.getElementById('rt-cycles-body');
  tbody.innerHTML = (d.cycles || []).map(c => {
    const pnlColor = c.net_pnl_usdt >= 0 ? '#26A69A' : '#EF5350';
    const sign     = c.net_pnl_usdt >= 0 ? '+' : '';
    const dt       = c.last_sell_time ? new Date(c.last_sell_time * 1000) : null;
    const ts       = dt
      ? dt.toLocaleDateString([], {month:'short',day:'numeric'}) + ' ' +
        dt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})
      : '—';
    const statusColor = c.is_closed ? '#4C525E' : '#FF9800';
    const statusLabel = c.is_closed ? 'Closed' : `Open (${c.remaining_qty.toPrecision(4)} left)`;
    return `<tr>
      <td style="font-weight:600;color:#D1D4DC">${c.symbol}</td>
      <td style="color:#26A69A">${c.buy_count}</td>
      <td style="color:#EF5350">${c.sell_count}</td>
      <td style="color:#787B86">$${fmt4(c.avg_buy_price)}</td>
      <td style="color:#787B86">$${fmt4(c.avg_sell_price)}</td>
      <td style="color:#EF5350">$${fmt2(c.total_bought_usdt)}</td>
      <td style="color:#26A69A">$${fmt2(c.total_sold_usdt)}</td>
      <td style="color:${pnlColor};font-weight:700">${sign}$${Math.abs(c.net_pnl_usdt).toFixed(2)}</td>
      <td style="color:${pnlColor};font-weight:700">${pct(c.net_pnl_pct)}</td>
      <td style="color:${statusColor};font-size:10px">${statusLabel}</td>
      <td style="color:#4C525E;font-size:10px">${ts}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="11" style="color:#4C525E;text-align:center">No completed cycles</td></tr>';
}

function renderAnalytics(d) {
  if (d.error) return;

  // Accumulate symbols from all_signals (never shrink the dropdown)
  const symSel = document.getElementById('an-symbol');
  const curSym = symSel.value;
  const newSyms = [...(d.all_symbols || []), ...Object.keys(d.by_symbol)];
  newSyms.forEach(s => {
    if (!knownSymbols.has(s)) {
      knownSymbols.add(s);
      const o = document.createElement('option');
      o.value = o.textContent = s;
      symSel.appendChild(o);
    }
  });
  // Restore selected value in case DOM order changed
  symSel.value = curSym;

  // Summary cards
  const n = d.total_trades;
  const setCard = (id, val, cls) => {
    const el = document.getElementById(id);
    el.textContent = val;
    el.className = 'an-card-val' + (cls ? ' ' + cls : '');
  };

  setCard('an-v-trades',  n || '—');
  setCard('an-v-winrate', n ? d.win_rate + '%' : '—',  n ? pctClass(d.win_rate - 50) : '');
  setCard('an-v-pnl',     n ? pct(d.total_pnl_pct) : '—', n ? pctClass(d.total_pnl_pct) : '');
  setCard('an-v-gain',    d.avg_gain_pct != null ? pct(d.avg_gain_pct) : '—', d.avg_gain_pct != null ? 'pos' : '');
  setCard('an-v-loss',    d.avg_loss_pct != null ? pct(d.avg_loss_pct) : '—', d.avg_loss_pct != null ? 'neg' : '');
  setCard('an-v-best',    n ? pct(d.best_trade_pct)  : '—', n ? pctClass(d.best_trade_pct)  : '');
  setCard('an-v-worst',   n ? pct(d.worst_trade_pct) : '—', n ? pctClass(d.worst_trade_pct) : '');
  setCard('an-v-sigs',    d.total_signals || '—');

  // Open positions
  const openWrap = document.getElementById('an-open-wrap');
  const openList = document.getElementById('an-open-list');
  if (d.open_positions && d.open_positions.length > 0) {
    openWrap.style.display = '';
    openList.innerHTML = d.open_positions.map(p => `
      <div class="an-pos-chip ${p.direction}">
        <span class="an-pos-dir ${p.direction}">${p.direction}</span>
        <span>${p.symbol} ${p.interval}</span>
        <span style="color:#787B86">@ ${fmtDisplay(p.entry_price)}</span>
        <span style="color:#4C525E;font-size:9px">${p.confidence}</span>
      </div>`).join('');
  } else {
    openWrap.style.display = 'none';
  }

  // By-symbol table
  const symBody = document.getElementById('an-by-symbol');
  symBody.innerHTML = Object.entries(d.by_symbol).map(([sym, s]) => `
    <tr>
      <td style="font-weight:600;color:#D1D4DC">${sym}</td>
      <td>${s.trades}</td>
      <td class="${pctClass(s.win_rate - 50)}">${s.win_rate}%</td>
      <td class="${pctClass(s.total_pnl)}">${pct(s.total_pnl)}</td>
      <td class="${pctClass(s.avg_pnl)}">${pct(s.avg_pnl)}</td>
    </tr>`).join('') || '<tr><td colspan="5" style="color:#4C525E;text-align:center">No data</td></tr>';

  // By-confidence table
  const confBody = document.getElementById('an-by-conf');
  const confColors = { HIGH: '#26A69A', MEDIUM: '#FF9800', LOW: '#787B86' };
  confBody.innerHTML = Object.entries(d.by_confidence).map(([conf, s]) => `
    <tr>
      <td style="font-weight:700;color:${confColors[conf]||'#D1D4DC'}">${conf}</td>
      <td>${s.trades}</td>
      <td class="${pctClass(s.win_rate - 50)}">${s.win_rate}%</td>
      <td class="${pctClass(s.total_pnl)}">${pct(s.total_pnl)}</td>
      <td class="${pctClass(s.avg_pnl)}">${pct(s.avg_pnl)}</td>
    </tr>`).join('') || '<tr><td colspan="5" style="color:#4C525E;text-align:center">No data</td></tr>';

  // Simulation
  if (d.simulation) renderSimulation(d.simulation);
  if (d.simulation?.engine_state) renderAdaptiveState(d.simulation.engine_state);

  // Trades table
  const tradesBody = document.getElementById('an-trades');
  const noTrades   = document.getElementById('an-no-trades');
  if (!d.trades || d.trades.length === 0) {
    tradesBody.innerHTML = '';
    noTrades.style.display = '';
  } else {
    noTrades.style.display = 'none';
    tradesBody.innerHTML = [...d.trades].reverse().map(t => {
      const dt = new Date(t.open_time * 1000);
      const ts = dt.toLocaleDateString([], {month:'short',day:'numeric'}) + ' ' +
                 dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
      const dirColor = t.direction === 'BUY' ? '#26A69A' : '#EF5350';
      const confC    = confColors[t.confidence] || '#D1D4DC';
      return `<tr>
        <td style="font-weight:600;color:#D1D4DC">${t.symbol}</td>
        <td style="color:#787B86">${t.interval}</td>
        <td style="color:${dirColor};font-weight:700">${t.direction}</td>
        <td style="color:${confC};font-size:9px;font-weight:700">${t.confidence}</td>
        <td>${fmtDisplay(t.entry_price)}</td>
        <td>${fmtDisplay(t.exit_price)}</td>
        <td class="${pctClass(t.pnl_pct)}" style="font-weight:700">${pct(t.pnl_pct)}</td>
        <td style="color:#4C525E;font-size:10px">${ts}</td>
      </tr>`;
    }).join('');
  }
}

function renderSimulation(s) {
  const setCard = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    el.className = 'an-card-val' + (cls ? ' ' + cls : '');
  };

  setCard('sim-v-final',   '$' + s.final_value_usdt.toFixed(2));
  setCard('sim-v-return',  pct(s.total_return_pct), pctClass(s.total_return_pct));
  setCard('sim-v-cash',    '$' + s.final_cash_usdt.toFixed(2));
  setCard('sim-v-holding', '$' + s.final_holding_usdt.toFixed(2));
  setCard('sim-v-count',   s.sim_trade_count || '—');

  // Skipped signal counts (adaptive DCA capping)
  const skipEl = document.getElementById('sim-skipped');
  if (skipEl) {
    const skB = s.skipped_buys  || 0;
    const skS = s.skipped_sells || 0;
    skipEl.textContent = (skB || skS)
      ? `DCA cap skipped: ${skB} buy${skB !== 1 ? 's' : ''}, ${skS} sell${skS !== 1 ? 's' : ''}`
      : '';
  }

  // Holdings chips
  const holdWrap = document.getElementById('sim-holdings-wrap');
  const holdEl   = document.getElementById('sim-holdings');
  const entries  = Object.entries(s.holdings || {});
  if (entries.length > 0) {
    holdWrap.style.display = '';
    holdEl.innerHTML = entries.map(([base, amt]) =>
      `<span style="background:#131722;border:1px solid #2A2E39;border-radius:4px;padding:3px 8px;font-size:11px;color:#D1D4DC">
        <span style="color:#FF9800;font-weight:700">${base}</span> ${amt.toFixed(6)}
      </span>`
    ).join('');
  } else {
    holdWrap.style.display = 'none';
  }

  // Sim trade log
  const tbody  = document.getElementById('sim-trades');
  const noData = document.getElementById('sim-no-data');
  const trades = s.sim_trades || [];

  if (trades.length === 0) {
    tbody.innerHTML = '';
    noData.style.display = '';
    return;
  }
  noData.style.display = 'none';

  const confColors = { HIGH: '#26A69A', MEDIUM: '#FF9800', LOW: '#787B86' };
  tbody.innerHTML = [...trades].reverse().map(t => {
    const dt = new Date(t.time * 1000);
    const ts = dt.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' +
               dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    const isBuy    = t.direction === 'BUY';
    const dirColor = isBuy ? '#26A69A' : '#EF5350';

    // Action label: BUY shows DCA entry#, SELL shows exit%
    const actionLabel = isBuy
      ? `BUY<sub style="font-size:8px;color:#787B86">#${t.dca_entry||1}</sub>`
      : `SELL<sub style="font-size:8px;color:#787B86">${t.exit_pct||100}%</sub>`;

    const usdtDelta = isBuy
      ? `<span style="color:#EF5350">-$${(t.usdt_spent||0).toFixed(2)}</span>`
      : `<span style="color:#26A69A">+$${(t.usdt_received||0).toFixed(2)}</span>`;

    // Avg Entry — only meaningful for SELLs (shows what we paid vs what we sold at)
    const avgEntryCell = isBuy
      ? '<td style="color:#4C525E">—</td>'
      : `<td style="color:#787B86;font-size:10px" title="Avg buy price">$${(t.avg_entry||t.price).toFixed(2)}</td>`;

    // Qty column
    const qty = isBuy
      ? (t.coins_bought || 0).toPrecision(4)
      : (t.coins_sold   || 0).toPrecision(4);

    // P&L cell only for sells
    const pnlCell = isBuy
      ? '<td style="color:#4C525E">—</td>'
      : `<td class="${pctClass(t.pnl_pct||0)}" style="font-weight:700">${pct(t.pnl_pct||0)}</td>`;

    return `<tr>
      <td style="font-weight:600;color:#D1D4DC">${t.symbol}</td>
      <td style="color:#787B86">${t.interval}</td>
      <td style="color:${dirColor};font-weight:700">${actionLabel}</td>
      <td style="color:${confColors[t.confidence]||'#D1D4DC'};font-size:9px;font-weight:700">${t.confidence}</td>
      <td>$${t.price.toFixed(2)}</td>
      ${avgEntryCell}
      <td style="color:#787B86;font-size:10px">${qty}</td>
      <td>${usdtDelta}</td>
      ${pnlCell}
      <td style="color:#B2B5BE">$${t.usdt_balance.toFixed(2)}</td>
      <td style="color:#D1D4DC;font-weight:600">$${t.portfolio_val.toFixed(2)}</td>
      <td style="color:#4C525E;font-size:10px">${ts}</td>
    </tr>`;
  }).join('');
}

function renderAdaptiveState(eng) {
  if (!eng) return;
  const setV = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    if (cls) el.className = 'an-card-val ' + cls;
  };
  setV('adp-winrate',  eng.win_rate_pct.toFixed(1) + '%',   eng.win_rate_pct >= 55 ? 'pos' : eng.win_rate_pct < 45 ? 'neg' : '');
  setV('adp-kelly',    eng.kelly_fraction_pct.toFixed(2) + '%');
  setV('adp-perf',     'x' + eng.perf_multiplier.toFixed(2), eng.perf_multiplier >= 1.0 ? 'pos' : 'neg');
  setV('adp-dd',       eng.drawdown_pct.toFixed(1) + '%',    eng.drawdown_pct >= 10 ? 'neg' : '');
  setV('adp-trades',   eng.trades_analyzed);
  setV('adp-rec-high', eng.rec_buy.HIGH + '%');
  setV('adp-rec-med',  eng.rec_buy.MEDIUM + '%');
  setV('adp-rec-low',  eng.rec_buy.LOW + '%');
  const cbEl = document.getElementById('adp-cb');
  if (cbEl) {
    cbEl.textContent  = eng.circuit_breaker ? 'ACTIVE' : 'OFF';
    cbEl.style.color  = eng.circuit_breaker ? '#EF5350' : '#26A69A';
  }
}

// ── Real CoinDCX Trades ───────────────────────────────────────────────────────

function loadDcxTrades() {
  const loadingEl = document.getElementById('dcx-trades-loading');
  const bodyEl    = document.getElementById('dcx-trades-body');
  const noEl      = document.getElementById('dcx-no-trades');
  const sumCards  = document.getElementById('dcx-summary-cards');
  const balWrap   = document.getElementById('dcx-balances-wrap');

  loadingEl.style.display = '';
  bodyEl.innerHTML = '';
  noEl.style.display = 'none';
  sumCards.style.display = 'none';
  balWrap.style.display = 'none';

  apiFetch('/api/trades/coindcx?limit=200')
    .then(r => r.json())
    .then(data => {
      loadingEl.style.display = 'none';
      if (data.error) {
        noEl.textContent = data.error;
        noEl.style.display = '';
        return;
      }
      renderDcxTrades(data);
    })
    .catch(err => {
      loadingEl.style.display = 'none';
      noEl.textContent = 'Failed to load trades. Check CoinDCX API keys in Account Settings.';
      noEl.style.display = '';
    });
}

function renderDcxTrades(data) {
  const setCard = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    el.className = 'an-card-val' + (cls ? ' ' + cls : '');
  };
  const fmt2 = n => n != null ? n.toFixed(2) : '—';
  const fmt4 = n => n != null ? n.toFixed(4) : '—';
  const fmt6 = n => n != null ? n.toFixed(6) : '—';

  const s = data.summary || {};

  // Summary cards
  const sumCards = document.getElementById('dcx-summary-cards');
  if (s.total_bought_usdt != null) {
    sumCards.style.display = '';
    setCard('dcx-v-bought-usdt', '$' + fmt2(s.total_bought_usdt));
    setCard('dcx-v-sold-usdt',   '$' + fmt2(s.total_sold_usdt));
    setCard('dcx-v-fee-usdt',    '$' + fmt4(s.total_fee_usdt));
    setCard('dcx-v-pnl-usdt',
      (s.net_pnl_usdt >= 0 ? '+$' : '-$') + Math.abs(s.net_pnl_usdt).toFixed(2),
      s.net_pnl_usdt >= 0 ? 'pos' : 'neg');
  }

  // Balances
  const balWrap = document.getElementById('dcx-balances-wrap');
  const balEl   = document.getElementById('dcx-balances');
  if (data.balances && data.balances.length > 0) {
    balWrap.style.display = '';
    balEl.innerHTML = data.balances.map(b => {
      const bal  = parseFloat(b.balance || 0);
      const lock = parseFloat(b.locked_balance || 0);
      const cur  = b.currency || '';
      const totalBal = bal + lock;
      return `<span style="background:#131722;border:1px solid #2A2E39;border-radius:4px;padding:3px 8px;font-size:11px;color:#D1D4DC">
        <span style="color:#FF9800;font-weight:700">${cur}</span>
        ${totalBal.toPrecision(5)}
        ${lock > 0 ? `<span style="color:#787B86;font-size:9px">(${lock.toPrecision(4)} locked)</span>` : ''}
      </span>`;
    }).join('');
  } else {
    balWrap.style.display = 'none';
  }

  // Trade table
  const bodyEl = document.getElementById('dcx-trades-body');
  const noEl   = document.getElementById('dcx-no-trades');
  const trades = data.trades || [];

  if (trades.length === 0) {
    bodyEl.innerHTML = '';
    noEl.textContent = 'No trades found in your CoinDCX account.';
    noEl.style.display = '';
    return;
  }
  noEl.style.display = 'none';

  bodyEl.innerHTML = trades.map(t => {
    const isBuy    = t.side === 'BUY';
    const dirColor = isBuy ? '#26A69A' : '#EF5350';
    const label    = isBuy ? 'Total Cost incl. fees' : 'Total Revenue after fees';
    const netColor = isBuy ? '#EF5350' : '#26A69A';
    const sign     = isBuy ? '-' : '+';
    const dt       = new Date(t.timestamp * 1000);
    const ts       = dt.toLocaleDateString([], {month:'short',day:'numeric'}) + ' ' +
                     dt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    return `<tr>
      <td style="font-weight:600;color:#D1D4DC">${t.market}</td>
      <td style="font-weight:700;color:${dirColor}">${t.side}</td>
      <td style="color:#D1D4DC">${t.quantity}</td>
      <td>$${fmt4(t.price_usdt)}</td>
      <td>$${fmt2(t.gross_usdt)}</td>
      <td style="color:#787B86;font-size:10px" title="Fee paid">$${fmt6(t.fee_usdt)}</td>
      <td style="color:${netColor};font-weight:600" title="${label}">${sign}$${fmt2(t.net_usdt)}</td>
      <td style="color:#4C525E;font-size:10px">${ts}</td>
    </tr>`;
  }).join('');
}

document.getElementById('dcx-trades-refresh').addEventListener('click', loadDcxTrades);

// ── Start ─────────────────────────────────────────────────────────────────────

// Sync interval dropdown and apply correct tier defaults on page load
(function initUI() {
  const sel = document.getElementById('iv-select');
  if (sel) {
    for (const opt of sel.options) opt.selected = opt.value === currentInterval;
  }
  // Always derive from tier — never trust stale localStorage on load
  applyTierToSensitivityBar(currentInterval);
})();

// ── Auth modal ────────────────────────────────────────────────────────────────

function showAuthModal() {
  const m = document.getElementById('auth-modal');
  m.style.display = 'flex';
}
function hideAuthModal() {
  document.getElementById('auth-modal').style.display = 'none';
}

// Tab switching
document.getElementById('tab-login').addEventListener('click', () => {
  document.getElementById('login-form').style.display  = '';
  document.getElementById('signup-form').style.display = 'none';
  document.getElementById('totp-setup').style.display  = 'none';
  document.getElementById('tab-login').classList.add('active');
  document.getElementById('tab-signup').classList.remove('active');
});
document.getElementById('tab-signup').addEventListener('click', () => {
  document.getElementById('login-form').style.display  = 'none';
  document.getElementById('signup-form').style.display = '';
  document.getElementById('totp-setup').style.display  = 'none';
  document.getElementById('tab-login').classList.remove('active');
  document.getElementById('tab-signup').classList.add('active');
});

// Login
document.getElementById('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  const body = {
    username:  document.getElementById('login-username').value.trim(),
    password:  document.getElementById('login-password').value,
    totp_code: document.getElementById('login-totp').value.trim(),
  };
  const r = await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) { errEl.textContent = data.error || data.detail || 'Login failed'; return; }
  authToken    = data.token;
  authUsername = data.username;
  authIsAdmin  = !!data.is_admin;
  localStorage.setItem('auth_token',    authToken);
  localStorage.setItem('auth_username', authUsername);
  localStorage.setItem('auth_is_admin', authIsAdmin);
  hideAuthModal();
  _showUserChip(authUsername);
  if (authIsAdmin) document.getElementById('admin-btn').style.display = '';
  initDashboard();
});

// Register
let _pendingRegUsername = '';
document.getElementById('signup-form').addEventListener('submit', async e => {
  e.preventDefault();
  const errEl = document.getElementById('signup-error');
  errEl.textContent = '';
  const body = {
    username: document.getElementById('reg-username').value.trim(),
    email:    document.getElementById('reg-email').value.trim(),
    password: document.getElementById('reg-password').value,
  };
  const r = await fetch('/api/auth/register', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) { errEl.textContent = data.error || data.detail || 'Registration failed'; return; }
  _pendingRegUsername = body.username;

  // Show QR setup step
  document.getElementById('signup-form').style.display = 'none';
  document.getElementById('totp-setup').style.display  = '';
  // Use server-generated QR code (SVG data URL)
  const qrImg = document.getElementById('totp-qr-img');
  qrImg.src = data.qr_data_url;
  document.getElementById('totp-secret-text').textContent = data.totp_secret;
});

// TOTP confirm
document.getElementById('totp-confirm-btn').addEventListener('click', async () => {
  const code  = document.getElementById('totp-confirm-code').value.trim();
  const errEl = document.getElementById('totp-error');
  errEl.textContent = '';
  if (!code) { errEl.textContent = 'Enter the 6-digit code'; return; }
  const r = await fetch('/api/auth/totp-confirm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: _pendingRegUsername, totp_code: code }),
  });
  const data = await r.json();
  if (!r.ok) { errEl.textContent = data.error || data.detail || 'Invalid code'; return; }
  // Auto-login after successful TOTP activation — prompt user to log in
  errEl.style.color = '#26A69A';
  errEl.textContent = '2FA activated! Please log in.';
  setTimeout(() => {
    document.getElementById('totp-setup').style.display  = 'none';
    document.getElementById('login-form').style.display  = '';
    document.getElementById('tab-login').classList.add('active');
    document.getElementById('tab-signup').classList.remove('active');
    errEl.style.color = '#EF5350';
    errEl.textContent = '';
  }, 1500);
});

// Logout
document.getElementById('logout-btn').addEventListener('click', () => {
  authToken = null; authUsername = null; authIsAdmin = false;
  localStorage.removeItem('auth_token');
  localStorage.removeItem('auth_username');
  localStorage.removeItem('auth_is_admin');
  if (ws) { ws.close(); ws = null; }
  _hideUserChip();
  document.getElementById('admin-btn').style.display = 'none';
  showAuthModal();
});

// ── Settings modal ─────────────────────────────────────────────────────────────

document.getElementById('settings-btn').addEventListener('click', async () => {
  if (!authToken) { showAuthModal(); return; }
  document.getElementById('settings-modal').style.display = 'flex';
  // Pre-fill stored values
  const r = await apiFetch('/api/profile/settings');
  if (!r.ok) return;
  const d = await r.json();
  if (d.telegram_token)   document.getElementById('set-tg-token').placeholder = 'Saved (hidden)';
  if (d.telegram_chat_id) document.getElementById('set-tg-chat').value        = d.telegram_chat_id || '';
  if (d.has_coindcx_key)  document.getElementById('set-dcx-key').placeholder  = 'Saved (hidden)';
  if (d.has_coindcx_secret) document.getElementById('set-dcx-secret').placeholder = 'Saved (hidden)';
});

document.getElementById('settings-close').addEventListener('click', () => {
  document.getElementById('settings-modal').style.display = 'none';
});

document.getElementById('settings-form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('settings-save-btn');
  const msg = document.getElementById('settings-msg');
  btn.disabled    = true;
  btn.textContent = 'Testing connection…';
  msg.textContent = '';

  const payload = {
    telegram_token:      document.getElementById('set-tg-token').value.trim()  || null,
    telegram_chat_id:    document.getElementById('set-tg-chat').value.trim()   || null,
    coindcx_api_key:     document.getElementById('set-dcx-key').value.trim()   || null,
    coindcx_api_secret:  document.getElementById('set-dcx-secret').value.trim() || null,
  };

  const r = await apiFetch('/api/profile/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await r.json().catch(() => ({}));
  if (r.ok) {
    msg.style.color = '#26A69A';
    msg.textContent = data.telegram_notified
      ? '✓ Saved! Telegram notification sent.'
      : '✓ Saved! (Telegram notification skipped — check token/chat ID)';
    // Clear sensitive inputs
    document.getElementById('set-dcx-key').value    = '';
    document.getElementById('set-dcx-secret').value = '';
    document.getElementById('set-tg-token').value   = '';
  } else {
    msg.style.color = '#EF5350';
    msg.textContent = data.error || data.detail || 'Save failed';
  }
  btn.disabled    = false;
  btn.textContent = 'Save & Test Connection';
});

// ── Auth gate — check token on load, gate dashboard ───────────────────────────

function initDashboard() {
  // Load all data + start WS stream
  loadHistoricalSignals();
  loadTriggers();
  fetchPortfolio();   // load once on login — user clicks ⟳ to refresh manually
  connect(currentSymbol, currentInterval);
}

document.getElementById('pf-refresh-btn').addEventListener('click', fetchPortfolio);

function _showUserChip(username) {
  const chip = document.getElementById('user-chip');
  if (chip) {
    chip.style.display = 'flex';
    const nameEl = document.getElementById('user-chip-name');
    if (nameEl) nameEl.textContent = username || 'User';
  }
}

function _hideUserChip() {
  const chip = document.getElementById('user-chip');
  if (chip) chip.style.display = 'none';
}

async function checkAuth() {
  if (!authToken) { showAuthModal(); return; }
  const r = await fetch('/api/auth/me', { headers: authHeaders() });
  if (!r.ok) {
    authToken = null; authUsername = null; authIsAdmin = false;
    localStorage.removeItem('auth_token');
    localStorage.removeItem('auth_username');
    localStorage.removeItem('auth_is_admin');
    _hideUserChip();
    document.getElementById('admin-btn').style.display = 'none';
    showAuthModal();
    return;
  }
  const me = await r.json();
  // Refresh username/admin from server in case localStorage is stale
  authUsername = me.username || authUsername;
  authIsAdmin  = !!me.is_admin;
  localStorage.setItem('auth_username', authUsername);
  localStorage.setItem('auth_is_admin', authIsAdmin);
  _showUserChip(authUsername);
  if (authIsAdmin) document.getElementById('admin-btn').style.display = '';
  initDashboard();
}

checkAuth();

// ── Admin Panel ───────────────────────────────────────────────────────────────

// Tab switching
document.querySelectorAll('.adm-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.adm-tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.adm-panel').forEach(p => p.style.display = 'none');
    btn.classList.add('active');
    const panel = document.getElementById('adm-tab-' + btn.dataset.tab);
    panel.style.display = btn.dataset.tab === 'browser' ? 'flex' : 'block';
    if (btn.dataset.tab === 'users') adminLoadUsers();
    if (btn.dataset.tab === 'danger') adminLoadStats();
    if (btn.dataset.tab === 'browser') admLoadTableList();
  });
});

// ── DB Browser ────────────────────────────────────────────────────────────────

let _admCurrentTable = null;
let _admCurrentPage  = 1;
let _admOrderBy      = null;
let _admOrderDir     = 'DESC';
let _admAllTables    = [];

async function admLoadTableList() {
  const list = document.getElementById('adm-table-list');
  list.innerHTML = '<div style="padding:8px 12px;color:#787B86;font-size:11px">Loading…</div>';
  const r = await apiFetch('/api/admin/tables');
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    list.innerHTML = `<div style="padding:8px 12px;color:#EF5350;font-size:11px">
      Error ${r.status}: ${err.detail || err.error || 'Failed'}<br>
      <span style="color:#787B86">Try logging out and back in.</span>
    </div>`;
    return;
  }
  _admAllTables = await r.json();
  if (!_admAllTables.length) {
    list.innerHTML = '<div style="padding:8px 12px;color:#787B86;font-size:11px">No tables found</div>';
    return;
  }
  list.innerHTML = _admAllTables.map(t =>
    `<div class="adm-table-item" data-table="${t.name}" onclick="admSelectTable('${t.name}')">
       <span>${t.name}</span>
       <span style="float:right;color:#787B86;font-size:10px">${t.row_count.toLocaleString()}</span>
     </div>`
  ).join('');
}

async function admSelectTable(name) {
  _admCurrentTable = name;
  _admCurrentPage  = 1;
  _admOrderBy      = null;
  _admOrderDir     = 'DESC';
  document.getElementById('adm-filter-col').value = '';
  document.getElementById('adm-filter-val').value = '';
  document.querySelectorAll('.adm-table-item').forEach(el => {
    el.classList.toggle('active', el.dataset.table === name);
  });
  document.getElementById('adm-no-table').style.display = 'none';
  document.getElementById('adm-data-table').style.display = '';
  await admLoadPage();
}

let _admLastData = null;  // cache for edit modal column list

async function admLoadPage() {
  if (!_admCurrentTable) return;
  const filterCol = document.getElementById('adm-filter-col').value.trim();
  const filterVal = document.getElementById('adm-filter-val').value.trim();
  const pageSize  = document.getElementById('adm-page-size').value;
  const params = new URLSearchParams({
    page: _admCurrentPage,
    page_size: pageSize,
    order_dir: _admOrderDir,
  });
  if (_admOrderBy) params.set('order_by', _admOrderBy);
  if (filterCol)   params.set('filter_col', filterCol);
  if (filterVal)   params.set('filter_val', filterVal);
  const r = await apiFetch(`/api/admin/table/${_admCurrentTable}?${params}`);
  if (!r.ok) return;
  const data = await r.json();
  _admLastData = data;

  document.getElementById('adm-table-name').textContent = data.table;
  document.getElementById('adm-page-info').textContent = `Page ${data.page} / ${data.pages}`;
  document.getElementById('adm-total-rows').textContent = `${data.total.toLocaleString()} rows total`;

  // Header: checkbox + rowid hidden + columns + actions
  const thead = document.getElementById('adm-data-thead');
  thead.innerHTML = `<tr>
    <th class="adm-th" style="width:30px"><input type="checkbox" id="adm-select-all" onclick="admToggleAll(this)"></th>
    ${data.columns.map(col =>
      `<th class="adm-th" onclick="admSortBy('${col}')">${col}${_admOrderBy===col?(_admOrderDir==='ASC'?' ↑':' ↓'):''}</th>`
    ).join('')}
    <th class="adm-th" style="min-width:100px">Actions</th>
  </tr>`;

  // Rows: data.rows[i][0] = rowid, data.rows[i][1..] = column values
  const tbody = document.getElementById('adm-data-tbody');
  if (!data.rows.length) {
    tbody.innerHTML = `<tr><td colspan="${data.columns.length + 2}" style="padding:20px;text-align:center;color:#787B86">No rows</td></tr>`;
    document.getElementById('adm-prev-btn').disabled = data.page <= 1;
    document.getElementById('adm-next-btn').disabled = data.page >= data.pages;
    return;
  }
  tbody.innerHTML = data.rows.map(row => {
    const rowid  = row[0];
    const cells  = row.slice(1);  // actual column values
    const cellsHtml = cells.map((cell, i) =>
      `<td class="adm-td" title="${cell ?? ''}">${cell ?? '<span style="color:#555">null</span>'}</td>`
    ).join('');
    return `<tr class="adm-tr" data-rowid="${rowid}">
      <td class="adm-td" style="text-align:center"><input type="checkbox" class="adm-row-cb" value="${rowid}"></td>
      ${cellsHtml}
      <td class="adm-td">
        <button onclick="admEditRow(${rowid}, this)"
          style="background:#1565C0;color:#fff;border:none;border-radius:3px;padding:2px 8px;font-size:11px;cursor:pointer;margin-right:3px">✏</button>
        <button onclick="admDeleteRow(${rowid})"
          style="background:#B71C1C;color:#fff;border:none;border-radius:3px;padding:2px 8px;font-size:11px;cursor:pointer">🗑</button>
      </td>
    </tr>`;
  }).join('');

  document.getElementById('adm-prev-btn').disabled = data.page <= 1;
  document.getElementById('adm-next-btn').disabled = data.page >= data.pages;
}

function admToggleAll(cb) {
  document.querySelectorAll('.adm-row-cb').forEach(c => c.checked = cb.checked);
}

function admSelectedRowids() {
  return [...document.querySelectorAll('.adm-row-cb:checked')].map(c => parseInt(c.value));
}

// ── Row edit / add modal ──────────────────────────────────────────────────────

function _admShowRowModal(title, rowData, onSave) {
  if (!_admLastData) return;
  const cols = _admLastData.columns;
  const modal = document.getElementById('adm-row-modal');
  document.getElementById('adm-row-modal-title').textContent = title;
  const form = document.getElementById('adm-row-form');
  form.innerHTML = cols.map(col => {
    const val = rowData ? (rowData[col] ?? '') : '';
    return `<div style="margin-bottom:10px">
      <label style="font-size:11px;color:#787B86;display:block;margin-bottom:3px">${col}</label>
      <input name="${col}" value="${String(val).replace(/"/g,'&quot;')}"
        style="width:100%;background:#131722;border:1px solid #2A2E39;color:#D1D4DC;
               border-radius:4px;padding:6px 8px;font-size:12px;box-sizing:border-box">
    </div>`;
  }).join('');
  modal.style.display = 'flex';
  document.getElementById('adm-row-save-btn').onclick = () => {
    const inputs = form.querySelectorAll('input');
    const data = {};
    inputs.forEach(inp => { data[inp.name] = inp.value === '' ? null : inp.value; });
    onSave(data);
  };
}

function admEditRow(rowid, btn) {
  if (!_admLastData) return;
  const cols  = _admLastData.columns;
  const tr    = btn.closest('tr');
  const cells = [...tr.querySelectorAll('td.adm-td')].slice(1, -1);  // skip checkbox + actions
  const rowData = {};
  cols.forEach((col, i) => { rowData[col] = cells[i]?.textContent?.trim() || null; });

  _admShowRowModal(`Edit row (rowid=${rowid})`, rowData, async (data) => {
    const r = await apiFetch(`/api/admin/table/${_admCurrentTable}/row/${rowid}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
    });
    const res = await r.json();
    document.getElementById('adm-row-modal').style.display = 'none';
    if (r.ok) admLoadPage();
    else alert('Error: ' + (res.error || 'Update failed'));
  });
}

function admAddRow() {
  _admShowRowModal('Add new row', null, async (data) => {
    const r = await apiFetch(`/api/admin/table/${_admCurrentTable}/row`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
    });
    const res = await r.json();
    document.getElementById('adm-row-modal').style.display = 'none';
    if (r.ok) { _admCurrentPage = 1; admLoadPage(); }
    else alert('Error: ' + (res.error || 'Insert failed'));
  });
}

async function admDeleteRow(rowid) {
  if (!confirm(`Delete row ${rowid} from "${_admCurrentTable}"?`)) return;
  const r = await apiFetch(`/api/admin/table/${_admCurrentTable}/row/${rowid}`, { method: 'DELETE' });
  if (r.ok) admLoadPage();
  else { const d = await r.json(); alert('Error: ' + (d.error || 'Delete failed')); }
}

async function admDeleteSelected() {
  const rowids = admSelectedRowids();
  if (!rowids.length) { alert('No rows selected.'); return; }
  if (!confirm(`Delete ${rowids.length} selected row(s) from "${_admCurrentTable}"?`)) return;
  const r = await apiFetch(`/api/admin/table/${_admCurrentTable}/rows`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rowids }),
  });
  const d = await r.json();
  if (r.ok) { admLoadPage(); admLoadTableList(); }
  else alert('Error: ' + (d.error || 'Delete failed'));
}

async function admDeleteAllRows() {
  if (!confirm(`Delete ALL rows from "${_admCurrentTable}"? This cannot be undone.`)) return;
  const r = await apiFetch(`/api/admin/table/${_admCurrentTable}/rows`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rowids: null }),
  });
  const d = await r.json();
  if (r.ok) { admLoadPage(); admLoadTableList(); }
  else alert('Error: ' + (d.error || 'Delete failed'));
}

document.getElementById('adm-row-modal-close').addEventListener('click', () => {
  document.getElementById('adm-row-modal').style.display = 'none';
});

function admSortBy(col) {
  if (_admOrderBy === col) {
    _admOrderDir = _admOrderDir === 'ASC' ? 'DESC' : 'ASC';
  } else {
    _admOrderBy  = col;
    _admOrderDir = 'DESC';
  }
  _admCurrentPage = 1;
  admLoadPage();
}

document.getElementById('adm-prev-btn').addEventListener('click', () => {
  if (_admCurrentPage > 1) { _admCurrentPage--; admLoadPage(); }
});
document.getElementById('adm-next-btn').addEventListener('click', () => {
  _admCurrentPage++;
  admLoadPage();
});
document.getElementById('adm-filter-btn').addEventListener('click', () => {
  _admCurrentPage = 1; admLoadPage();
});
document.getElementById('adm-filter-clear').addEventListener('click', () => {
  document.getElementById('adm-filter-col').value = '';
  document.getElementById('adm-filter-val').value = '';
  _admCurrentPage = 1; admLoadPage();
});
document.getElementById('adm-page-size').addEventListener('change', () => {
  _admCurrentPage = 1; admLoadPage();
});

// ── Query editor ─────────────────────────────────────────────────────────────

document.getElementById('adm-query-run').addEventListener('click', async () => {
  const sql = document.getElementById('adm-query-input').value.trim();
  if (!sql) return;
  document.getElementById('adm-query-status').textContent = 'Running…';
  const r = await apiFetch('/api/admin/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql }),
  });
  const data = await r.json();
  if (!r.ok) {
    document.getElementById('adm-query-status').textContent = '✗ ' + (data.error || 'Error');
    document.getElementById('adm-query-thead').innerHTML = '';
    document.getElementById('adm-query-tbody').innerHTML = '';
    return;
  }
  document.getElementById('adm-query-status').textContent =
    `✓ ${data.count} row${data.count !== 1 ? 's' : ''}`;
  document.getElementById('adm-query-thead').innerHTML =
    '<tr>' + data.columns.map(c => `<th class="adm-th">${c}</th>`).join('') + '</tr>';
  document.getElementById('adm-query-tbody').innerHTML = data.rows.map(row =>
    `<tr class="adm-tr">${row.map(cell =>
      `<td class="adm-td">${cell ?? '<span style="color:#555">null</span>'}</td>`
    ).join('')}</tr>`
  ).join('');
});

// Allow Ctrl+Enter to run query
document.getElementById('adm-query-input').addEventListener('keydown', e => {
  if (e.ctrlKey && e.key === 'Enter') document.getElementById('adm-query-run').click();
});

// ── Users tab ─────────────────────────────────────────────────────────────────

async function adminLoadUsers() {
  const r = await apiFetch('/api/admin/users');
  if (!r.ok) return;
  const users = await r.json();
  document.getElementById('admin-users-body').innerHTML = users.map(u => `
    <tr style="border-bottom:1px solid #2A2E39">
      <td style="padding:7px 10px;color:#787B86">${u.id}</td>
      <td style="padding:7px 10px;font-weight:600;color:#D1D4DC">${u.username}</td>
      <td style="padding:7px 10px;color:#787B86;font-size:11px">${u.email}</td>
      <td style="padding:7px 10px;text-align:center">${u.totp_enabled ? '✅' : '—'}</td>
      <td style="padding:7px 10px;text-align:center">${u.is_admin ? '⚡' : '—'}</td>
      <td style="padding:7px 10px;text-align:center;color:#787B86;font-size:11px">${new Date(u.created_at*1000).toLocaleDateString()}</td>
      <td style="padding:7px 10px;display:flex;gap:4px;justify-content:center">
        <button onclick="adminToggleAdmin(${u.id},'${u.username}')"
          style="background:#4A148C;color:#CE93D8;border:none;border-radius:3px;padding:3px 8px;font-size:11px;cursor:pointer">
          ${u.is_admin ? 'Revoke Admin' : 'Make Admin'}
        </button>
        <button onclick="adminDeleteUser(${u.id},'${u.username}')"
          style="background:#B71C1C;color:#fff;border:none;border-radius:3px;padding:3px 8px;font-size:11px;cursor:pointer">
          Delete
        </button>
      </td>
    </tr>
  `).join('');
}

async function adminToggleAdmin(userId, username) {
  if (!confirm(`Toggle admin status for "${username}"?`)) return;
  const r = await apiFetch(`/api/admin/users/${userId}/toggle-admin`, { method: 'PUT' });
  const data = await r.json();
  if (r.ok) adminLoadUsers(); else alert(data.error || 'Failed');
}

async function adminDeleteUser(userId, username) {
  if (!confirm(`Permanently delete "${username}" and all their data? Cannot be undone.`)) return;
  const r = await apiFetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
  const data = await r.json();
  if (r.ok) adminLoadUsers(); else alert(data.error || 'Failed');
}

document.getElementById('admin-refresh-users-btn').addEventListener('click', adminLoadUsers);

// ── Danger zone ───────────────────────────────────────────────────────────────

function _statCard(label, value) {
  return `<div style="background:#131722;border:1px solid #2A2E39;border-radius:6px;padding:8px 14px;min-width:90px;text-align:center">
    <div style="font-size:18px;font-weight:700;color:#CE93D8">${value}</div>
    <div style="font-size:10px;color:#787B86;margin-top:2px;text-transform:uppercase">${label}</div>
  </div>`;
}

async function adminLoadStats() {
  const r = await apiFetch('/api/admin/db-stats');
  if (!r.ok) return;
  const stats = await r.json();
  document.getElementById('admin-db-stats').innerHTML =
    Object.entries(stats).map(([k, v]) => _statCard(k, v)).join('');
}

document.getElementById('admin-refresh-btn').addEventListener('click', adminLoadStats);

document.getElementById('admin-clear-signals-btn').addEventListener('click', async () => {
  if (!confirm('Delete ALL signals from the database? Cannot be undone.')) return;
  const r = await apiFetch('/api/admin/clear-signals', { method: 'POST' });
  const data = await r.json();
  if (r.ok) {
    document.getElementById('admin-clear-msg').textContent = `✓ Deleted ${data.deleted} signals.`;
    adminLoadStats();
  }
});

document.getElementById('admin-clear-all-btn').addEventListener('click', async () => {
  if (!confirm('Wipe signals, candles, orders and trade history? Users/triggers kept. Cannot be undone.')) return;
  const r = await apiFetch('/api/admin/clear-all', { method: 'POST' });
  const data = await r.json();
  if (r.ok) {
    const counts = Object.entries(data.deleted).map(([k,v]) => `${k}: ${v}`).join(', ');
    document.getElementById('admin-clear-msg').textContent = `✓ ${counts}`;
    adminLoadStats();
  }
});

// ── Open / close admin modal ──────────────────────────────────────────────────

document.getElementById('admin-btn').addEventListener('click', () => {
  document.getElementById('admin-modal').style.display = 'flex';
  admLoadTableList();  // default: DB Browser tab
});

document.getElementById('admin-close').addEventListener('click', () => {
  document.getElementById('admin-modal').style.display = 'none';
});
