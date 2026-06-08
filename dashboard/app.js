/**
 * CIMES – Chili Intelligent Monitoring and Environmental Sensing System
 * app.js – Dashboard logic with LIVE API + WebSocket realtime + DEMO fallback
 *
 * Connects to FastAPI backend at /api/* endpoints.
 * Uses WebSocket for instant realtime updates.
 * Falls back to HTTP polling, then demo simulation if backend is unavailable.
 */

'use strict';

// ── Agricultural Thresholds (synced: firmware ↔ bridge.py ↔ dashboard) ──
const THRESH = {
  vwc: {
    critical_low:  0.15,   // < 15 % → CRITICAL LOW
    pump_on:       0.25,   // < 25 % → PUMP ON
    optimal_low:   0.60,   // 60–80 % optimal range
    optimal_high:  0.80,   // > 80 % → PUMP OFF / STOP
    critical_high: 0.85,   // > 85 % → CRITICAL HIGH
  },
  temp: {
    critical_low:  18.0,   // < 18°C → CRITICAL
    optimal_low:   22.0,
    optimal_high:  30.0,
    critical_high: 35.0,   // > 35°C → CRITICAL
  },
  ec: {
    optimal_low:   0.8,    // dS/m
    optimal_high:  2.0,
    critical_high: 3.0,    // > 3.0 → CRITICAL
  },
};

// ── Config ────────────────────────────────────────────────────
const API_BASE        = '';      // same origin when served from backend
const UPDATE_MS       = 4000;    // fallback polling interval (used when WS is down)
const HISTORY_POINTS  = 48;
const WS_RECONNECT_MS = 3000;   // WebSocket reconnect delay

// ── State ─────────────────────────────────────────────────────
let totalReadings = 0;
let _tick = 0;
let pumpState = 'STANDBY'; // 'ON' | 'OFF' | 'STANDBY'
let isLiveMode = false;     // true = fetching from API, false = demo simulation
let apiCheckInterval = null;
let wsConnected = false;    // true = WebSocket is active
let ws = null;              // WebSocket instance
let wsReconnectTimer = null;
let pollingTimer = null;    // fallback polling setInterval ID

const history = { labels: [], temp: [], ec: [], moisture: [] };

// ── DOM helpers ───────────────────────────────────────────────
const $ = id => document.getElementById(id);

// ── Clock ─────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  $('clockDisplay').textContent = now.toLocaleTimeString('id-ID', { hour12: false });
  $('dateDisplay').textContent  = now.toLocaleDateString('id-ID', {
    weekday: 'short', year: 'numeric', month: 'short', day: 'numeric'
  });
}
setInterval(updateClock, 1000);
updateClock();

// ── Mode Indicator ────────────────────────────────────────────
function setMode(live) {
  isLiveMode = live;
  const badge = $('modeBadge');
  if (!badge) return;

  if (live) {
    badge.className = 'mode-badge live';
    badge.innerHTML = '<span class="mode-dot live-dot"></span><span>LIVE</span>';
    $('xmppStatus').textContent = '● Online';
    $('xmppStatus').className = 'status-val online';
    $('dbStatus').textContent = '● Connected';
    $('dbStatus').className = 'status-val online';
    $('nodeStatus').textContent = '● Active';
    $('nodeStatus').className = 'status-val online';
  } else {
    badge.className = 'mode-badge demo';
    badge.innerHTML = '<span class="mode-dot demo-dot"></span><span>DEMO</span>';
    $('xmppStatus').textContent = '○ Demo';
    $('xmppStatus').className = 'status-val offline';
    $('dbStatus').textContent = '○ Demo';
    $('dbStatus').className = 'status-val offline';
    $('nodeStatus').textContent = '○ Simulated';
    $('nodeStatus').className = 'status-val offline';
  }
}

// ── Chart.js global defaults (soft palette) ───────────────────
Chart.defaults.color      = '#A0937E';
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.font.size   = 11;

// ── Optimal-zone annotation plugin (pure canvas, no library) ──
const zonePlugin = {
  id: 'optimalZone',
  beforeDatasetsDraw(chart, _, opts) {
    if (!opts || opts.yMin === undefined) return;
    const { ctx, chartArea: { left, right, top, bottom }, scales: { y } } = chart;
    const yTop = y.getPixelForValue(opts.yMax);
    const yBot = y.getPixelForValue(opts.yMin);
    ctx.save();
    ctx.fillStyle = opts.fillColor || 'rgba(74,153,128,0.08)';
    ctx.fillRect(left, yTop, right - left, yBot - yTop);
    // dashed borders
    ctx.strokeStyle = opts.borderColor || 'rgba(74,153,128,0.35)';
    ctx.lineWidth   = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(left, yTop); ctx.lineTo(right, yTop); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(left, yBot); ctx.lineTo(right, yBot); ctx.stroke();
    ctx.restore();
  }
};
Chart.register(zonePlugin);

// ── Chart factory ─────────────────────────────────────────────
function makeLineChart(canvasId, label, borderColor, bgColor, zoneOpts) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  const gradient = ctx.createLinearGradient(0, 0, 0, 200);
  gradient.addColorStop(0, bgColor);
  gradient.addColorStop(1, 'rgba(255,255,255,0)');

  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label,
        data: [],
        borderColor,
        backgroundColor: gradient,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: borderColor,
        tension: 0.4,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 700 },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        optimalZone: zoneOpts || false,
        tooltip: {
          backgroundColor: 'rgba(250,249,246,0.95)',
          borderColor: 'rgba(200,190,170,0.50)',
          borderWidth: 1,
          padding: 10,
          titleColor: '#2C2418',
          bodyColor: borderColor,
          boxShadow: '0 4px 16px rgba(160,140,100,0.15)',
        }
      },
      scales: {
        x: {
          grid: { color: 'rgba(0,0,0,0.04)' },
          ticks: { maxTicksLimit: 8, maxRotation: 0, color: '#A0937E' },
        },
        y: {
          grid: { color: 'rgba(0,0,0,0.04)' },
          ticks: { maxTicksLimit: 6, color: '#A0937E' },
        }
      }
    }
  });
}

const tempChart = makeLineChart(
  'tempChart', 'Temperature (°C)',
  '#6B8F71', 'rgba(107,143,113,0.18)',
  { yMin: THRESH.temp.optimal_low, yMax: THRESH.temp.optimal_high,
    fillColor: 'rgba(107,143,113,0.08)', borderColor: 'rgba(107,143,113,0.35)' }
);
const ecChart = makeLineChart(
  'ecChart', 'EC (dS/m)',
  '#5B7FA6', 'rgba(91,127,166,0.18)',
  { yMin: THRESH.ec.optimal_low, yMax: THRESH.ec.optimal_high,
    fillColor: 'rgba(91,127,166,0.08)', borderColor: 'rgba(91,127,166,0.35)' }
);
const moistureChart = makeLineChart(
  'moistureChart', 'VWC (m³/m³)',
  '#C1714F', 'rgba(193,113,79,0.18)',
  { yMin: THRESH.vwc.optimal_low, yMax: THRESH.vwc.optimal_high,
    fillColor: 'rgba(193,113,79,0.10)', borderColor: 'rgba(193,113,79,0.40)' }
);

// ── Push to charts ────────────────────────────────────────────
function pushHistory(label, temp, ec, vwc) {
  if (history.labels.length >= HISTORY_POINTS) {
    ['labels','temp','ec','moisture'].forEach(k => history[k].shift());
  }
  history.labels.push(label);
  history.temp.push(temp);
  history.ec.push(ec);
  history.moisture.push(vwc);

  [[tempChart, history.temp], [ecChart, history.ec], [moistureChart, history.moisture]]
    .forEach(([chart, arr]) => {
      chart.data.labels = [...history.labels];
      chart.data.datasets[0].data = [...arr];
      chart.update('none');
    });
}

// ── Load history from API (bulk populate charts) ──────────────
function loadHistoryFromAPI(data) {
  // Clear existing
  ['labels','temp','ec','moisture'].forEach(k => { history[k] = []; });

  data.forEach(row => {
    const d = new Date(row.ts);
    const label = d.toLocaleTimeString('id-ID', {
      hour12: false, hour: '2-digit', minute: '2-digit'
    });
    history.labels.push(label);
    history.temp.push(row.temp);
    history.ec.push(row.ec);
    history.moisture.push(row.vwc);
  });

  // Update all charts at once
  [[tempChart, history.temp], [ecChart, history.ec], [moistureChart, history.moisture]]
    .forEach(([chart, arr]) => {
      chart.data.labels = [...history.labels];
      chart.data.datasets[0].data = [...arr];
      chart.update('none');
    });
}

// ── Load alerts from API ──────────────────────────────────────
function loadAlertsFromAPI(alerts) {
  const log = $('alertLog');
  if (alerts.length === 0) {
    log.innerHTML = '<div class="no-alerts">No alerts – plantation conditions nominal 🌿</div>';
    return;
  }

  log.innerHTML = '';
  alerts.forEach(a => {
    const item = document.createElement('div');
    const level = a.alert_level === 'CRITICAL' ? 'critical' : 'warning';
    item.className = `alert-item ${level}`;
    const t = new Date(a.ts).toLocaleTimeString('id-ID', { hour12: false });
    item.innerHTML = `<div>${a.message}</div><div class="alert-time">${t}</div>`;
    log.appendChild(item);
  });
}

// ── Classify VWC status ───────────────────────────────────────
function classifyVWC(vwc) {
  const T = THRESH.vwc;
  if (vwc < T.critical_low)  return { label: 'CRITICAL LOW',  cls: 'critical', pump: 'ON'  };
  if (vwc < T.pump_on)       return { label: 'DRY – PUMP ON', cls: 'warning',  pump: 'ON'  };
  if (vwc < T.optimal_low)   return { label: 'LOW',           cls: 'warning',  pump: 'ON'  };
  if (vwc <= T.optimal_high) return { label: 'OPTIMAL',       cls: 'optimal',  pump: 'OFF' };
  if (vwc <= T.critical_high)return { label: 'HIGH',          cls: 'warning',  pump: 'OFF' };
  return                             { label: 'CRITICAL HIGH', cls: 'critical', pump: 'OFF' };
}

function classifyTemp(temp) {
  const T = THRESH.temp;
  if (temp < T.critical_low)  return { label: 'CRITICAL LOW',  cls: 'critical' };
  if (temp < T.optimal_low)   return { label: 'COOL',          cls: 'warning'  };
  if (temp <= T.optimal_high) return { label: 'OPTIMAL',       cls: 'optimal'  };
  if (temp < T.critical_high) return { label: 'WARM',          cls: 'warning'  };
  return                              { label: 'CRITICAL HIGH', cls: 'critical' };
}

function classifyEC(ec) {
  const T = THRESH.ec;
  if (ec < T.optimal_low)   return { label: 'LOW',          cls: 'warning'  };
  if (ec <= T.optimal_high) return { label: 'OPTIMAL',      cls: 'optimal'  };
  if (ec < T.critical_high) return { label: 'HIGH',         cls: 'warning'  };
  return                            { label: 'CRITICAL HIGH',cls: 'critical' };
}

// ── Liquid Gauge ──────────────────────────────────────────────
function setLiquidGauge(vwc, statusCls) {
  const pct = Math.min(Math.max(vwc, 0), 1) * 100;
  $('liquidFill').style.height = pct + '%';
  $('vwcValue').textContent = vwc.toFixed(4);
  $('vwcPct').textContent   = pct.toFixed(1) + ' %';

  const colorMap = {
    optimal:  'linear-gradient(180deg,rgba(193,113,79,0.55) 0%,rgba(160,80,40,0.75) 100%)',
    warning:  'linear-gradient(180deg,rgba(201,169,110,0.65) 0%,rgba(160,120,40,0.80) 100%)',
    critical: 'linear-gradient(180deg,rgba(185,79,60,0.65) 0%,rgba(150,40,20,0.85) 100%)',
  };
  $('liquidFill').style.background = colorMap[statusCls] || colorMap.optimal;
}

// ── Badge helper ──────────────────────────────────────────────
function setBadge(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className   = `card-badge ${cls}`;
}

// ── Radial ring ───────────────────────────────────────────────
function setRadialRing(arcId, value, min, max) {
  const pct    = Math.min(Math.max((value - min) / (max - min), 0), 1);
  $(arcId).style.strokeDashoffset = 326.73 * (1 - pct);
}

// ── Temperature widget ────────────────────────────────────────
function setTemp(temp, status) {
  $('tempValue').textContent = temp.toFixed(1);
  setRadialRing('tempArc', temp, 0, 50);
  $('tempBar').style.width   = Math.min((temp / 50) * 100, 100) + '%';
  setBadge('tempBadge', status.label, status.cls);
  $('tempRangeLabel').textContent = status.label;
  $('tempRangeLabel').style.color =
    status.cls === 'optimal' ? 'var(--mint)' :
    status.cls === 'warning' ? 'var(--sand)' : 'var(--rust)';
}

// ── EC widget ─────────────────────────────────────────────────
function setEC(ec, status) {
  $('ecValue').textContent = ec.toFixed(4);
  setRadialRing('ecArc', ec, 0, 4);
  $('ecBar').style.width   = Math.min((ec / 4) * 100, 100) + '%';
  setBadge('ecBadge', status.label, status.cls);
  $('ecRangeLabel').textContent = status.label;
  $('ecRangeLabel').style.color =
    status.cls === 'optimal' ? 'var(--mint)' :
    status.cls === 'warning' ? 'var(--sand)' : 'var(--rust)';
}

// ── Pump status ───────────────────────────────────────────────
function updatePump(vwcStatus) {
  const newState = vwcStatus.pump; // 'ON' | 'OFF'
  if (newState !== pumpState) {
    pumpState = newState;
    addAlert('pump',
      newState === 'ON'
        ? '💧 Irrigation pump ACTIVATED – soil moisture below threshold'
        : '✅ Irrigation pump STOPPED – moisture restored'
    );
  }
  const badge = $('pumpBadge');
  badge.className = `pump-badge ${newState.toLowerCase()}`;
  badge.textContent =
    newState === 'ON'  ? '💧 PUMP: ACTIVE – Irrigating' :
    newState === 'OFF' ? '✅ PUMP: STOPPED' :
                         '💧 PUMP STATUS: STANDBY';
}

// ── Alert Log ─────────────────────────────────────────────────
const _alertState = { vwc: null, temp: null, ec: null };

function addAlert(level, message) {
  const log  = $('alertLog');
  const noEl = log.querySelector('.no-alerts');
  if (noEl) noEl.remove();

  const item = document.createElement('div');
  item.className = `alert-item ${level}`;
  const t = new Date().toLocaleTimeString('id-ID', { hour12: false });
  item.innerHTML = `<div>${message}</div><div class="alert-time">${t}</div>`;
  log.prepend(item);
  while (log.children.length > 35) log.lastChild.remove();

  const banner = $('alertBanner');
  $('alertText').textContent = message;
  banner.style.display = 'block';
  setTimeout(() => banner.style.display = 'none', 5000);
}

function evaluateAlerts(vwcSt, tempSt, ecSt, vwc, temp, ec) {
  if (vwcSt.cls !== _alertState.vwc) {
    _alertState.vwc = vwcSt.cls;
    if (vwcSt.cls !== 'optimal')
      addAlert(vwcSt.cls, `Moisture ${vwcSt.label}: ${(vwc*100).toFixed(1)}% – ${vwcSt.cls === 'critical' ? 'Immediate action!' : 'Monitor closely'}`);
    else
      addAlert('optimal', `Moisture restored to optimal: ${(vwc*100).toFixed(1)}%`);
  }
  if (tempSt.cls !== _alertState.temp) {
    _alertState.temp = tempSt.cls;
    if (tempSt.cls !== 'optimal')
      addAlert(tempSt.cls, `Temperature ${tempSt.label}: ${temp.toFixed(1)}°C`);
  }
  if (ecSt.cls !== _alertState.ec) {
    _alertState.ec = ecSt.cls;
    if (ecSt.cls !== 'optimal')
      addAlert(ecSt.cls, `EC ${ecSt.label}: ${ec.toFixed(4)} dS/m`);
  }
}

$('clearAlerts').addEventListener('click', () => {
  $('alertLog').innerHTML = '<div class="no-alerts">No alerts – plantation conditions nominal 🌿</div>';
});

// ══════════════════════════════════════════════════════════════
//  DATA FETCHING — LIVE API + DEMO FALLBACK
// ══════════════════════════════════════════════════════════════

// ── Check if API is available ─────────────────────────────────
async function checkAPI() {
  try {
    const res = await fetch(`${API_BASE}/api/health`, { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      const data = await res.json();
      if (data.status === 'ok') {
        if (!isLiveMode) {
          setMode(true);
          await loadInitialData();
        }
        return true;
      }
    }
  } catch (e) {
    // API not available
  }
  if (isLiveMode) {
    setMode(false);
  }
  return false;
}

// ── Load initial data from API (history + alerts + stats) ─────
async function loadInitialData() {
  try {
    // Load history for charts
    const histRes = await fetch(`${API_BASE}/api/history?hours=24`);
    if (histRes.ok) {
      const histData = await histRes.json();
      if (histData.length > 0) {
        loadHistoryFromAPI(histData);
      }
    }

    // Load alerts
    const alertRes = await fetch(`${API_BASE}/api/alerts?limit=30`);
    if (alertRes.ok) {
      const alertData = await alertRes.json();
      loadAlertsFromAPI(alertData);
    }

    // Load stats
    const statsRes = await fetch(`${API_BASE}/api/stats`);
    if (statsRes.ok) {
      const stats = await statsRes.json();
      totalReadings = stats.total_readings || 0;
      $('totalReadings').textContent = totalReadings.toLocaleString();
    }
  } catch (e) {
    console.warn('CIMES: Failed to load initial data:', e);
  }
}

// ── Fetch latest reading from API ─────────────────────────────
async function fetchLiveReading() {
  const res = await fetch(`${API_BASE}/api/latest`, { signal: AbortSignal.timeout(3000) });
  if (!res.ok) throw new Error(`API ${res.status}`);
  const data = await res.json();
  return {
    vwc:  parseFloat(data.vwc),
    temp: parseFloat(data.temp),
    ec:   parseFloat(data.ec),
  };
}

// ── Simulated data (demo mode fallback) ───────────────────────
function fetchDemoReading() {
  _tick++;
  const t = _tick * 0.09;
  return Promise.resolve({
    vwc:  parseFloat((0.55 + 0.22*Math.sin(t*0.7) + (Math.random()-0.5)*0.02).toFixed(4)),
    temp: parseFloat((26.0 + 5.0*Math.sin(t*0.5)  + (Math.random()-0.5)*0.4 ).toFixed(2)),
    ec:   parseFloat((1.20 + 0.40*Math.cos(t*0.4) + (Math.random()-0.5)*0.03).toFixed(4)),
  });
}

// ── Unified fetch ─────────────────────────────────────────────
async function fetchLatestReading() {
  if (isLiveMode) {
    try {
      return await fetchLiveReading();
    } catch (e) {
      console.warn('CIMES: Live fetch failed, falling back to demo:', e.message);
      setMode(false);
      return fetchDemoReading();
    }
  }
  return fetchDemoReading();
}

// ══════════════════════════════════════════════════════════════
//  WEBSOCKET – REALTIME CONNECTION
// ══════════════════════════════════════════════════════════════

function getWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return; // already connected or connecting
  }

  const url = getWsUrl();
  console.log('CIMES: Connecting WebSocket →', url);
  ws = new WebSocket(url);

  ws.onopen = () => {
    console.log('CIMES: ✅ WebSocket connected');
    wsConnected = true;
    setMode(true);

    // Update WebSocket status indicator
    $('wsStatus').textContent = '● Connected';
    $('wsStatus').className = 'status-val online';

    // Stop polling since we have realtime now
    if (pollingTimer) {
      clearInterval(pollingTimer);
      pollingTimer = null;
      console.log('CIMES: Polling stopped – using WebSocket');
    }

    // Clear reconnect timer
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }

    // Send periodic pings to keep connection alive
    ws._pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send('ping');
      }
    }, 30000);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);

      if (data.type === 'sensor_reading') {
        handleRealtimeReading(data);
      } else if (data.type === 'alert') {
        handleRealtimeAlert(data);
      }
      // ignore pong
    } catch (e) {
      console.warn('CIMES: WS message parse error:', e);
    }
  };

  ws.onclose = (event) => {
    console.warn('CIMES: WebSocket closed', event.code, event.reason);
    wsConnected = false;
    if (ws._pingInterval) clearInterval(ws._pingInterval);
    ws = null;

    // Update WebSocket status indicator
    $('wsStatus').textContent = '○ Polling';
    $('wsStatus').className = 'status-val offline';

    // Fall back to polling
    startPollingFallback();

    // Schedule reconnect
    wsReconnectTimer = setTimeout(connectWebSocket, WS_RECONNECT_MS);
  };

  ws.onerror = (error) => {
    console.warn('CIMES: WebSocket error:', error);
    // onclose will fire after this
  };
}

// Handle realtime sensor reading from WebSocket
function handleRealtimeReading(data) {
  const vwc  = parseFloat(data.vwc);
  const temp = parseFloat(data.temp);
  const ec   = parseFloat(data.ec);
  const label = new Date().toLocaleTimeString('id-ID', {
    hour12: false, hour: '2-digit', minute: '2-digit'
  });

  totalReadings++;
  $('totalReadings').textContent  = totalReadings.toLocaleString();
  $('lastReadingTime').textContent = new Date().toLocaleTimeString('id-ID', { hour12: false });

  const vwcSt  = classifyVWC(vwc);
  const tempSt = classifyTemp(temp);
  const ecSt   = classifyEC(ec);

  setLiquidGauge(vwc, vwcSt.cls);
  setBadge('moistureBadge', vwcSt.label, vwcSt.cls);
  setTemp(temp, tempSt);
  setEC(ec, ecSt);
  updatePump(vwcSt);
  pushHistory(label, temp, ec, vwc);
  evaluateAlerts(vwcSt, tempSt, ecSt, vwc, temp, ec);
}

// Handle realtime alert from WebSocket
function handleRealtimeAlert(data) {
  const level = data.alert_level === 'CRITICAL' ? 'critical' : 'warning';
  addAlert(level, data.message || `${data.alert_type}: ${data.current_value}`);
}

// ── Polling fallback (only when WebSocket is disconnected) ────
function startPollingFallback() {
  if (pollingTimer) return; // already polling
  console.log('CIMES: Starting polling fallback');
  pollingTimer = setInterval(updateDashboard, UPDATE_MS);
}

// ── Main update loop (HTTP fallback) ──────────────────────────
async function updateDashboard() {
  try {
    const { vwc, temp, ec } = await fetchLatestReading();
    const label = new Date().toLocaleTimeString('id-ID', {
      hour12: false, hour: '2-digit', minute: '2-digit'
    });

    totalReadings++;
    $('totalReadings').textContent  = totalReadings.toLocaleString();
    $('lastReadingTime').textContent = new Date().toLocaleTimeString('id-ID', { hour12: false });

    const vwcSt  = classifyVWC(vwc);
    const tempSt = classifyTemp(temp);
    const ecSt   = classifyEC(ec);

    setLiquidGauge(vwc, vwcSt.cls);
    setBadge('moistureBadge', vwcSt.label, vwcSt.cls);
    setTemp(temp, tempSt);
    setEC(ec, ecSt);
    updatePump(vwcSt);
    pushHistory(label, temp, ec, vwc);
    evaluateAlerts(vwcSt, tempSt, ecSt, vwc, temp, ec);

  } catch (err) {
    console.error('CIMES update error:', err);
    $('nodeStatus').textContent = '● Error';
    $('nodeStatus').className   = 'status-val offline';
  }
}

// ── Initialize ────────────────────────────────────────────────
async function init() {
  // Try to connect to API first
  const apiAvailable = await checkAPI();
  if (!apiAvailable) {
    setMode(false);
  }

  // Try WebSocket connection for realtime
  connectWebSocket();

  // Start polling as initial fallback (will be stopped if WS connects)
  updateDashboard();
  startPollingFallback();

  // Periodically check if API becomes available (every 15s)
  setInterval(checkAPI, 15000);
}

init();
