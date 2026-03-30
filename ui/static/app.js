/* Crypto Signal Dashboard */

// ── State ─────────────────────────────────────────────────────────────────────

let ws               = null;
let currentSymbol    = 'BTCUSDT';
let currentInterval  = '1m';
let markers          = [];
let currentEditId    = null;   // null = create mode, number = edit existing trigger

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

  addSignalCard(msg, msg.trigger_matched);
  pushNotification(msg);
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

// ── Signal card ───────────────────────────────────────────────────────────────

function addSignalCard(msg, triggerMatch = false) {
  const list = document.getElementById('signal-list');
  list.querySelector('.no-sig')?.remove();

  const time  = new Date(msg.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const price = msg.entry_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const card = document.createElement('div');
  card.className = `signal-card ${msg.direction}${triggerMatch ? ' trigger-match' : ''}`;
  card.innerHTML = `
    <div class="sc-row">
      <span class="sc-dir">${msg.direction}</span>
      <span class="sc-conf-${msg.confidence}">${msg.confidence}</span>
    </div>
    <div class="sc-price">$${price}</div>
    <div class="sc-time">${time}</div>
    ${msg.reasons.length ? `<div class="sc-reason">${msg.reasons.join(' &nbsp;·&nbsp; ')}</div>` : ''}
    <div class="sc-trend">${msg.trend_note}</div>
  `;
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
  ws = new WebSocket(`${wsProto}//${location.host}/ws?symbol=${symbol.toLowerCase()}&interval=${interval}`);

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

document.querySelectorAll('.iv-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.iv-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentInterval = btn.dataset.iv;
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

// ── Triggers panel ────────────────────────────────────────────────────────────

function loadTriggers() {
  fetch('/api/triggers')
    .then(r => r.json())
    .then(renderTriggers)
    .catch(() => {});
}

function renderTriggers(list) {
  const el = document.getElementById('triggers-list');
  if (!Array.isArray(list) || list.length === 0) {
    el.innerHTML = '<div class="trig-empty">No triggers</div>';
    return;
  }
  el.innerHTML = '';
  list.forEach(t => {
    const row = document.createElement('div');
    row.className = `trig-row${t.active ? '' : ' inactive'}`;
    row.dataset.id = t.id;
    row.innerHTML =
      `<span class="trig-sym">${t.symbol}</span>` +
      `<span class="trig-iv">${t.interval}</span>` +
      `<span class="trig-conf trig-conf-${t.min_confidence}">${t.min_confidence}</span>` +
      `<span class="trig-edit" title="Edit">✏</span>` +
      `<span class="trig-toggle" title="${t.active ? 'Disable' : 'Enable'}">${t.active ? '✓' : '○'}</span>` +
      `<span class="trig-del" title="Delete">✕</span>`;

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

function openEditForm(t) {
  currentEditId = t.id;
  document.getElementById('trig-sym').value = t.symbol;
  for (const opt of document.getElementById('trig-iv').options)
    opt.selected = opt.value === t.interval;
  for (const opt of document.getElementById('trig-conf').options)
    opt.selected = opt.value === t.min_confidence;
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
    for (const opt of document.getElementById('trig-iv').options)
      opt.selected = opt.value === currentInterval;
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

  fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol: sym, interval: iv, min_confidence: conf }),
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
      btn.textContent = currentEditId ? 'Update Trigger' : 'Save Trigger';
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

let analyticsOpen = false;

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

function loadAnalytics() {
  const sym      = document.getElementById('an-symbol').value;
  const interval = document.getElementById('an-interval').value;
  const conf     = document.getElementById('an-conf').value;
  const period   = document.getElementById('an-period').value;
  const params   = new URLSearchParams({ symbol: sym, interval, confidence: conf, period });
  fetch(`/api/analytics?${params}`)
    .then(r => r.json())
    .then(renderAnalytics)
    .catch(() => {});
}

function renderAnalytics(d) {
  if (d.error) return;

  // Populate symbol dropdown from by_symbol keys (keep ALL + seen symbols)
  const symSel = document.getElementById('an-symbol');
  const curSym = symSel.value;
  const known  = new Set(Object.keys(d.by_symbol));
  [...symSel.options].forEach(o => { if (o.value !== 'ALL') o.remove(); });
  known.forEach(s => {
    const o = document.createElement('option');
    o.value = o.textContent = s;
    if (s === curSym) o.selected = true;
    symSel.appendChild(o);
  });

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

// ── Start ─────────────────────────────────────────────────────────────────────

connect(currentSymbol, currentInterval);
