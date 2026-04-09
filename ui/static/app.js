/* Crypto Signal Dashboard */

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
let sigConfFilter = '';   // '' = ALL, 'HIGH', 'MEDIUM', 'LOW'
let sigTrigFilter = '';   // '' = ALL, 'RSI', 'Stoch', 'OBV'
let selectedCards = new Set();

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
  loadHistoricalSignals(currentSymbol, currentInterval);
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

  // Sidebar card + push notification only when a trigger matches
  if (msg.trigger_matched) {
    addSignalCard(msg, true);
    pushNotification(msg);
  }
  mainChart.timeScale().scrollToRealTime();
  if (analyticsOpen) loadAnalytics();
}

// ── Historical signal pre-load ────────────────────────────────────────────────

function loadHistoricalSignals(symbol, interval) {
  fetch(`/api/signals/history?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=50`)
    .then(r => r.json())
    .then(signals => {
      if (!Array.isArray(signals) || signals.length === 0) return;
      // Signals are newest-first from API; prepend in that order so newest is on top
      signals.forEach(s => {
        // Normalise field names from DB row to match WebSocket signal shape
        addSignalCard({
          direction:   s.direction,
          confidence:  s.confidence,
          entry_price: s.entry_price,
          time:        s.open_time / 1000,   // open_time is ms epoch
          reasons:     Array.isArray(s.reasons) ? s.reasons : [],
          trend_note:  s.trend_note || '',
          macd_val:    s.macd_val,
          adx_val:     s.adx_val,
        });
      });
    })
    .catch(() => {});  // DB not configured — silently ignore
}

// ── Portfolio panel ───────────────────────────────────────────────────────────

function fetchPortfolio() {
  fetch('/api/portfolio')
    .then(r => r.json())
    .then(data => {
      if (!Array.isArray(data)) return;
      const list = document.getElementById('portfolio-list');
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
      document.getElementById('pf-updated').textContent =
        'Updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    })
    .catch(() => {
      document.getElementById('pf-updated').textContent = 'Unavailable';
    });
}

// Poll portfolio every 30 seconds
fetchPortfolio();
setInterval(fetchPortfolio, 30000);

// ── Signal card + filter + manage ────────────────────────────────────────────

function getCardTriggers(reasons) {
  const cats = [];
  if (reasons.some(r => r.startsWith('RSI')))   cats.push('RSI');
  if (reasons.some(r => r.startsWith('Stoch'))) cats.push('Stoch');
  if (reasons.some(r => r.startsWith('OBV')))   cats.push('OBV');
  return cats;
}

function cardVisible(card) {
  const confOk = !sigConfFilter || card.dataset.conf === sigConfFilter;
  const trigs  = (card.dataset.triggers || '').split(',').filter(Boolean);
  const trigOk = !sigTrigFilter || trigs.includes(sigTrigFilter);
  return confOk && trigOk;
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
}

function deleteByTrigger(trigger) {
  closeDeleteMenus();
  document.querySelectorAll('#signal-list .signal-card').forEach(card => {
    if ((card.dataset.triggers || '').split(',').includes(trigger)) {
      markers = markers.filter(m => m.time !== parseFloat(card.dataset.time));
      selectedCards.delete(card);
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
}

function deleteSelected() {
  [...selectedCards].forEach(card => {
    markers = markers.filter(m => m.time !== parseFloat(card.dataset.time));
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
  const price   = msg.entry_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
    </div>
    <div class="sc-price">$${price}</div>
    <div class="sc-time">${time}</div>
    ${reasons.length ? `<div class="sc-reason">${reasons.join(' &nbsp;·&nbsp; ')}</div>` : ''}
    <div class="sc-trend">${msg.trend_note || ''}</div>
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
    body: `$${msg.entry_price.toFixed(2)}  ·  ${msg.reasons.join(', ')}`,
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
    await fetch('/api/triggers/bulk-delete', {
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

document.querySelectorAll('.tf-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    sigTrigFilter = btn.dataset.tf;
    applyAllFilters();
  });
});

document.getElementById('sig-del-all').addEventListener('click',     () => deleteAllSignals());
document.getElementById('sig-del-sel-btn').addEventListener('click', () => deleteSelected());
document.getElementById('sig-desel-btn').addEventListener('click', () => {
  selectedCards.forEach(c => c.classList.remove('sc-selected'));
  selectedCards.clear();
  updateBulkBar();
});

// ── Triggers panel ────────────────────────────────────────────────────────────

function loadTriggers() {
  fetch('/api/triggers')
    .then(r => r.json())
    .then(renderTriggers)
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
    const adxHint = t.adx_threshold != null ? ` adx≥${t.adx_threshold}` : '';
    const cdHint  = t.cooldown_bars  != null ? ` cd${t.cooldown_bars}`   : '';
    row.innerHTML =
      `<input type="checkbox" class="trig-cb trig-row-cb" data-id="${t.id}" />` +
      `<span class="trig-sym">${t.symbol}</span>` +
      `<span class="trig-iv">${t.interval}</span>` +
      `<span class="trig-conf trig-conf-${t.min_confidence}">${t.min_confidence}</span>` +
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
      fetch(`/api/triggers/${t.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active: !t.active }),
      }).then(r => { if (r.ok) loadTriggers(); });
    });

    row.querySelector('.trig-del').addEventListener('click', () => {
      fetch(`/api/triggers/${t.id}`, { method: 'DELETE' })
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
  document.getElementById('trig-sym').value = t.symbol;
  for (const opt of document.getElementById('trig-iv').options)
    opt.selected = opt.value === t.interval;
  for (const opt of document.getElementById('trig-conf').options)
    opt.selected = opt.value === t.min_confidence;
  // Populate ADX / cooldown (show stored value or blank for "tier default")
  document.getElementById('trig-adx').value = t.adx_threshold != null ? t.adx_threshold : '';
  document.getElementById('trig-cd').value  = t.cooldown_bars  != null ? t.cooldown_bars  : '';
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
    document.getElementById('trig-sym').value = currentSymbol;
    document.getElementById('trig-adx').value = '';
    document.getElementById('trig-cd').value  = '';
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
  const sym    = document.getElementById('trig-sym').value.trim().toUpperCase() || currentSymbol;
  const iv     = document.getElementById('trig-iv').value;
  const conf   = document.getElementById('trig-conf').value;
  const editId = currentEditId;

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

  const adxRaw = document.getElementById('trig-adx').value.trim();
  const cdRaw  = document.getElementById('trig-cd').value.trim();
  const payload = {
    symbol: sym, interval: iv, min_confidence: conf,
    adx_threshold: adxRaw !== '' ? parseFloat(adxRaw) : null,
    cooldown_bars: cdRaw  !== '' ? parseInt(cdRaw, 10) : null,
  };

  fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  .then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  })
  .then(() => {
    document.getElementById('trig-add-form').classList.remove('open');
    document.getElementById('trig-sym').value = '';
    btn.disabled    = false;
    btn.textContent = 'Save Trigger';
    currentEditId   = null;
    loadTriggers();
  })
  .catch(() => {
    btn.disabled    = false;
    btn.textContent = '✕ Failed – retry';
    setTimeout(() => {
      btn.textContent = editId ? 'Update Trigger' : 'Save Trigger';
    }, 2500);
  });
});

// Initial load
loadTriggers();

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

function pct(v, decimals = 2) {
  const s = (v >= 0 ? '+' : '') + v.toFixed(decimals) + '%';
  return s;
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
  fetch(`/api/analytics?${params}`)
    .then(r => r.json())
    .then(renderAnalytics)
    .catch(() => {});
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
  setCard('an-v-gain',    d.avg_gain_pct ? pct(d.avg_gain_pct) : '—', 'pos');
  setCard('an-v-loss',    d.avg_loss_pct ? pct(d.avg_loss_pct) : '—', 'neg');
  setCard('an-v-best',    d.best_trade_pct  ? pct(d.best_trade_pct)  : '—', 'pos');
  setCard('an-v-worst',   d.worst_trade_pct ? pct(d.worst_trade_pct) : '—', 'neg');
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
        <span style="color:#787B86">@ $${p.entry_price.toFixed(2)}</span>
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
        <td>$${t.entry_price.toFixed(2)}</td>
        <td>$${t.exit_price.toFixed(2)}</td>
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
      <td>${usdtDelta}</td>
      ${pnlCell}
      <td style="color:#B2B5BE">$${t.usdt_balance.toFixed(2)}</td>
      <td style="color:#D1D4DC;font-weight:600">$${t.portfolio_val.toFixed(2)}</td>
      <td style="color:#4C525E;font-size:10px">${ts}</td>
    </tr>`;
  }).join('');
}

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

connect(currentSymbol, currentInterval);
