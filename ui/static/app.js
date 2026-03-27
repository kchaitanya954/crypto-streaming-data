/* Crypto Signal Dashboard */

// ── State ─────────────────────────────────────────────────────────────────────

let ws               = null;
let currentSymbol    = 'BTCUSDT';
let currentInterval  = '1m';
let markers          = [];

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

  addSignalCard(msg);
  pushNotification(msg);
  mainChart.timeScale().scrollToRealTime();
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

function addSignalCard(msg) {
  const list = document.getElementById('signal-list');
  list.querySelector('.no-sig')?.remove();

  const time  = new Date(msg.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const price = msg.entry_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const card = document.createElement('div');
  card.className = `signal-card ${msg.direction}`;
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

  ws = new WebSocket(`ws://${location.host}/ws?symbol=${symbol.toLowerCase()}&interval=${interval}`);

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

// ── Start ─────────────────────────────────────────────────────────────────────

connect(currentSymbol, currentInterval);
