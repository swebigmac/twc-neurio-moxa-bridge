#!/usr/bin/env python3
"""Single-file HTTP UI for the TWC Neurio simulator.

The web app deliberately uses only Python's standard library.  That keeps the
field deployment simple on a small Debian box: no Node build, no Flask, no
database, and no package manager beyond pyserial for the serial simulator.

Responsibilities:

* Serve a desktop-oriented status/control page over plain HTTP.
* Scan the local subnet for Tesla Wall Connector Gen 3 HTTP APIs.
* Poll known Wall Connectors periodically and show live charging status.
* Read/write the simulator values file used by the Modbus RTU process.

Security model: this is for a trusted local LAN.  There is no authentication.
Do not expose it to the internet.
"""

import ipaddress
import json
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HOST = "0.0.0.0"
PORT = 8080
SCAN_TIMEOUT = 1.2
MAX_WORKERS = 64
KNOWN_DEVICES_PATH = "/etc/twc-neurio-sim/known_wall_connectors.json"
VALUES_PATH = "/etc/twc-neurio-sim/values.json"
DEFAULT_VOLTAGE = 230.0

def iso_now():
    """Timestamp helper used in API responses and values.json writes."""
    return datetime.now().astimezone().isoformat(timespec="seconds")

INDEX_HTML = r'''<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>TWC Neurio Control</title>
  <style>
    :root {
      --bg: #f6f6f4;
      --panel: #ffffff;
      --panel-soft: #efefef;
      --text: #171a20;
      --muted: #5c5e62;
      --line: #e2e3e3;
      --green: #12bd00;
      --blue: #3e6ae1;
      --danger: #d93025;
      --shadow: 0 18px 50px rgba(0,0,0,.08);
      --radius: 8px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      letter-spacing: 0;
    }
    .shell { max-width: 1180px; margin: 0 auto; padding: 34px 34px 28px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 28px; }
    .brand { display: flex; align-items: center; gap: 18px; }
    .mark {
      width: 48px; height: 48px; border-radius: 8px; background: #171a20; color: #fff;
      display: grid; place-items: center; font-weight: 700; letter-spacing: 3px; font-size: 13px;
    }
    h1 { font-size: 30px; line-height: 1.05; margin: 0; font-weight: 700; }
    .sub { color: var(--muted); margin-top: 6px; font-size: 15px; }
    .status-pill { display: flex; align-items: center; gap: 10px; color: var(--muted); font-weight: 600; }
    .dot { width: 13px; height: 13px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 4px rgba(18,189,0,.13); }
    .layout { display: grid; grid-template-columns: 380px 1fr; gap: 22px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); }
    .hero { padding: 28px; min-height: 520px; position: relative; overflow: hidden; }
    .charger-art { height: 260px; position: relative; margin: 26px 0 20px; }
    .charger {
      width: 112px; height: 226px; border-radius: 38px 38px 24px 24px; background: linear-gradient(110deg,#fff 0%,#f7f7f7 54%,#e2e3e3 55%,#fafafa 100%);
      border: 2px solid #ddd; box-shadow: inset 0 0 22px rgba(0,0,0,.06), 0 22px 45px rgba(0,0,0,.10);
      position: absolute; left: 118px; top: 8px;
    }
    .charger:before { content: "TESLA"; position: absolute; top: 60px; left: 34px; color: #b7b8ba; font-size: 12px; font-weight: 700; letter-spacing: 4px; }
    .led { position: absolute; width: 7px; height: 24px; border-radius: 8px; background: #00d632; left: 52px; top: 111px; box-shadow: 0 0 22px #00d632; }
    .cable { position: absolute; width: 170px; height: 118px; left: 199px; top: 155px; border: 18px solid #171a20; border-left: 0; border-bottom: 0; border-radius: 0 70px 0 0; transform: rotate(13deg); opacity: .94; }
    .headline { display: flex; align-items: center; gap: 14px; font-size: 28px; font-weight: 700; margin-top: 8px; }
    .check { width: 36px; height: 36px; border-radius: 50%; background: var(--green); color: white; display: grid; place-items: center; font-size: 25px; font-weight: 800; }
    .primary { width: 100%; border: 0; background: var(--blue); color: white; border-radius: 6px; height: 52px; font-size: 16px; font-weight: 700; cursor: pointer; margin-top: 24px; }
    .primary:disabled { opacity: .65; cursor: default; }
    .secondary { border: 0; background: var(--panel-soft); color: var(--text); border-radius: 6px; height: 46px; padding: 0 18px; font-weight: 700; cursor: pointer; white-space: nowrap; }
    .scan-action { background: var(--blue); color: #fff; }
    .secondary:disabled { opacity: .65; cursor: default; }
    .neurio-panel { margin-top: 24px; padding-top: 22px; border-top: 1px solid var(--line); }
    .neurio-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .neurio-title { font-size: 20px; font-weight: 700; margin: 0; }
    .neurio-serial { color: var(--muted); font-size: 13px; margin-top: 3px; }
    .mode-select { height: 36px; border: 1px solid var(--line); border-radius: 6px; background: #fff; padding: 0 10px; font: inherit; font-weight: 700; }
    .neurio-art { display: grid; grid-template-columns: 86px 1fr; gap: 0; align-items: center; margin: 10px -6px 18px; min-height: 168px; }
    .ct-block { height: 152px; border-radius: 0 22px 22px 0; background: #202124; position: relative; box-shadow: inset -10px 0 18px rgba(255,255,255,.04); }
    .ct-slot { position: absolute; left: 43px; width: 46px; height: 34px; border-radius: 7px; background: #f8f8f8; display: grid; place-items: center; color: #2f3338; font-size: 24px; font-weight: 700; box-shadow: 0 1px 4px rgba(0,0,0,.18); }
    .ct-slot:nth-child(1) { top: 10px; }
    .ct-slot:nth-child(2) { top: 59px; }
    .ct-slot:nth-child(3) { top: 108px; }
    .ct-phase { position: absolute; left: 18px; font-size: 19px; font-weight: 700; }
    .ct-phase.p1 { top: 15px; color: #a0a3a8; }
    .ct-phase.p2 { top: 64px; color: #e34f45; }
    .ct-phase.p3 { top: 113px; color: #3e6ae1; }
    .wire-board { display: grid; gap: 15px; padding: 3px 0; }
    .wire-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; min-height: 34px; }
    .wire-line { height: 4px; border-radius: 999px; background: #a9abad; position: relative; }
    .wire-line:after { content: ""; position: absolute; right: -10px; top: -9px; width: 22px; height: 22px; border: 7px solid #5d6066; border-left: 0; border-radius: 0 14px 14px 0; }
    .wire-value { min-width: 78px; text-align: right; font-weight: 700; color: var(--text); }
    .wire-sub { display: block; color: var(--muted); font-size: 12px; font-weight: 600; margin-top: 2px; }
    .phase-list { display: grid; gap: 12px; }
    .phase-row { display: grid; grid-template-columns: 34px 1fr 76px; gap: 10px; align-items: center; }
    .phase-label { font-weight: 700; color: var(--muted); }
    .phase-track { height: 8px; background: var(--panel-soft); border-radius: 999px; overflow: hidden; }
    .phase-fill { height: 100%; width: 0%; border-radius: 999px; background: var(--blue); transition: width .25s ease; }
    .amp-input { width: 76px; height: 38px; border: 1px solid var(--line); border-radius: 6px; padding: 0 8px; font: inherit; text-align: right; background: #fff; }
    .amp-input:disabled { color: var(--muted); background: var(--panel-soft); }
    .neurio-foot { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; margin-top: 16px; }
    .total-current { color: var(--muted); font-size: 14px; }
    .save-small { border: 0; background: #171a20; color: #fff; border-radius: 6px; height: 40px; padding: 0 14px; font-weight: 700; cursor: pointer; }
    .save-small:disabled { opacity: .55; cursor: default; }
    .history-panel { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 16px; }
    .history-title { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 13px; font-weight: 700; margin-bottom: 10px; }
    .spark-row { display: grid; grid-template-columns: 28px 1fr 48px; gap: 10px; align-items: center; margin-top: 10px; }
    .spark-row canvas { width: 100%; height: 34px; display: block; border-bottom: 1px solid var(--line); }
    .spark-label { color: var(--muted); font-size: 12px; font-weight: 800; }
    .spark-value { text-align: right; font-size: 12px; font-weight: 800; color: var(--text); }
    .content { display: grid; gap: 18px; }
    .scan-card { padding: 24px; }
    .card-title { font-size: 24px; font-weight: 700; margin: 0 0 6px; }
    .card-copy { margin: 0; color: var(--muted); max-width: 650px; }
    .toolbar { display: flex; gap: 12px; align-items: center; margin-top: 22px; }
    .input {
      height: 46px; border: 1px solid var(--line); border-radius: 6px; padding: 0 13px; font: inherit; background: #fff; min-width: 220px;
    }
    .results { overflow: hidden; }
    .row { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; padding: 18px 22px; border-top: 1px solid var(--line); }
    .row:first-child { border-top: 0; }
    .wc-name { font-size: 19px; font-weight: 700; }
    .wc-meta { color: var(--muted); margin-top: 3px; }
    .metric-strip { display: flex; gap: 18px; color: var(--muted); margin-top: 9px; flex-wrap: wrap; }
    .metric b { color: var(--text); }
    .tag { display: inline-flex; align-items: center; gap: 8px; height: 34px; padding: 0 12px; border-radius: 999px; background: #eff8ed; color: #18820d; font-weight: 700; }
    .tag.offline { background: #f4eeee; color: #9b1c12; }
    .tag.offline .dot { background: var(--danger); box-shadow: 0 0 0 4px rgba(217,48,37,.12); }
    .empty { padding: 28px 22px; color: var(--muted); border-top: 1px solid var(--line); }
    .debug { background: #111318; color: #d9dde7; border-radius: var(--radius); overflow: hidden; border: 1px solid #252933; }
    .debug-head { height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 16px; background: #171a20; font-weight: 700; }
    pre { margin: 0; padding: 16px; min-height: 220px; max-height: 330px; overflow: auto; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; white-space: pre-wrap; }
    @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } .hero { min-height: 420px; } }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand">
        <div class="mark">T</div>
        <div>
          <h1>Wall Connector Control</h1>
          <div class="sub">Neurio simulator och lokal Wall Connector-detektering</div>
        </div>
      </div>
      <div class="status-pill"><span class="dot"></span><span id="serverStatus">Webserver online</span></div>
    </header>

    <main class="layout">
      <section class="panel hero">
        <div class="headline"><span class="check">✓</span><span>System Ready</span></div>
        <div class="sub">Butler svarar på Neurio-register via Moxa och kan hitta laddboxar via Wi-Fi API.</div>
        <div class="charger-art">
          <div class="charger"><div class="led"></div></div>
          <div class="cable"></div>
        </div>

        <div class="neurio-panel">
          <div class="neurio-head">
            <div>
              <h2 class="neurio-title">Neurio Meter</h2>
              <div class="neurio-serial">VAH4810AB0231</div>
            </div>
            <select id="neurioMode" class="mode-select" aria-label="Neurio mode">
              <option value="manual">Manuell</option>
              <option value="auto">Auto</option>
            </select>
          </div>
          <div class="neurio-art" aria-hidden="true">
            <div class="ct-block">
              <div class="ct-slot">1</div>
              <div class="ct-slot">2</div>
              <div class="ct-slot">3</div>
              <div class="ct-phase p1">A</div>
              <div class="ct-phase p2">B</div>
              <div class="ct-phase p3">C</div>
            </div>
            <div class="wire-board">
              <div class="wire-row"><div class="wire-line"></div><div class="wire-value"><span id="wireAmp1">- A</span><span id="wireWatt1" class="wire-sub">- W</span></div></div>
              <div class="wire-row"><div class="wire-line"></div><div class="wire-value"><span id="wireAmp2">- A</span><span id="wireWatt2" class="wire-sub">- W</span></div></div>
              <div class="wire-row"><div class="wire-line"></div><div class="wire-value"><span id="wireAmp3">- A</span><span id="wireWatt3" class="wire-sub">- W</span></div></div>
            </div>
          </div>
          <div class="phase-list">
            <label class="phase-row"><span class="phase-label">L1</span><span class="phase-track"><span id="phaseFill1" class="phase-fill"></span></span><input id="phase1" class="amp-input" type="number" min="0" max="80" step="0.1" /></label>
            <label class="phase-row"><span class="phase-label">L2</span><span class="phase-track"><span id="phaseFill2" class="phase-fill"></span></span><input id="phase2" class="amp-input" type="number" min="0" max="80" step="0.1" /></label>
            <label class="phase-row"><span class="phase-label">L3</span><span class="phase-track"><span id="phaseFill3" class="phase-fill"></span></span><input id="phase3" class="amp-input" type="number" min="0" max="80" step="0.1" /></label>
          </div>
          <div class="neurio-foot">
            <div id="neurioTotal" class="total-current">- A totalt</div>
            <button id="saveNeurioBtn" class="save-small">Sätt ström</button>
          </div>
          <div class="history-panel">
            <div class="history-title"><span>Senaste timmen</span><span id="historyScale">0-32 A</span></div>
            <div class="spark-row"><span class="spark-label">L1</span><canvas id="spark1" width="260" height="44"></canvas><span id="sparkValue1" class="spark-value">- A</span></div>
            <div class="spark-row"><span class="spark-label">L2</span><canvas id="spark2" width="260" height="44"></canvas><span id="sparkValue2" class="spark-value">- A</span></div>
            <div class="spark-row"><span class="spark-label">L3</span><canvas id="spark3" width="260" height="44"></canvas><span id="sparkValue3" class="spark-value">- A</span></div>
          </div>
        </div>
      </section>

      <section class="content">
        <div class="panel scan-card">
          <h2 class="card-title">Lokalt nätverk</h2>
          <p class="card-copy">Skannar subnetet efter Tesla Wall Connector Gen 3 genom att anropa deras lokala API.</p>
          <div class="toolbar">
            <input id="subnetInput" class="input" placeholder="Auto, e.g. 192.0.2.0/24" />
            <button id="refreshBtn" class="secondary">Använd auto</button>
            <button id="scanBtn" class="secondary scan-action">Scanna Wall Connectors</button>
          </div>
        </div>

        <div class="panel results" id="results">
          <div class="empty">Inga laddboxar skannade ännu.</div>
        </div>

        <div class="debug">
          <div class="debug-head"><span>Debug</span><span id="scanState">idle</span></div>
          <pre id="debugLog">Klicka på “Scanna Wall Connectors” för att börja.</pre>
        </div>
      </section>
    </main>
  </div>

<script>
const scanBtn = document.getElementById('scanBtn');
const refreshBtn = document.getElementById('refreshBtn');
const subnetInput = document.getElementById('subnetInput');
const results = document.getElementById('results');
const debugLog = document.getElementById('debugLog');
const scanState = document.getElementById('scanState');
const neurioMode = document.getElementById('neurioMode');
const phaseInputs = [document.getElementById('phase1'), document.getElementById('phase2'), document.getElementById('phase3')];
const phaseFills = [document.getElementById('phaseFill1'), document.getElementById('phaseFill2'), document.getElementById('phaseFill3')];
const neurioTotal = document.getElementById('neurioTotal');
const saveNeurioBtn = document.getElementById('saveNeurioBtn');
const wireAmps = [document.getElementById('wireAmp1'), document.getElementById('wireAmp2'), document.getElementById('wireAmp3')];
const wireWatts = [document.getElementById('wireWatt1'), document.getElementById('wireWatt2'), document.getElementById('wireWatt3')];
const sparkCanvases = [document.getElementById('spark1'), document.getElementById('spark2'), document.getElementById('spark3')];
const sparkValues = [document.getElementById('sparkValue1'), document.getElementById('sparkValue2'), document.getElementById('sparkValue3')];
const historyScale = document.getElementById('historyScale');
let refreshTimer = null;
let neurioTimer = null;
let lastDevices = [];
let editingNeurio = false;
let neurioHistory = [];

function log(text) {
  const stamp = new Date().toLocaleTimeString();
  debugLog.textContent += `\n${stamp}  ${text}`;
  debugLog.scrollTop = debugLog.scrollHeight;
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function fmtTime(value) {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString();
}

function statusMeta(d) {
  const parts = [];
  const checked = fmtTime(d.checked_at);
  const seen = fmtTime(d.last_seen_at);
  if (checked) parts.push(`kontrollerad ${checked}`);
  if (d.online === false && seen) parts.push(`senast online ${seen}`);
  if (d.online === false && d.status_error) parts.push(`fel: ${esc(d.status_error)}`);
  return parts.length ? `<div class="wc-meta">${parts.join(' · ')}</div>` : '';
}

function renderDevices(devices) {
  lastDevices = devices || [];
  if (!devices.length) {
    results.innerHTML = '<div class="empty">Inga Wall Connectors hittades på subnetet.</div>';
    return;
  }
  results.innerHTML = devices.map(d => `
    <div class="row">
      <div>
        <div class="wc-name">Wall Connector</div>
        <div class="wc-meta">${esc(d.ip)}${d.version ? ' · firmware ' + esc(d.version) : ''}${d.serial ? ' · ' + esc(d.serial) : ''}</div>
        ${statusMeta(d)}
        <div class="metric-strip">
          <span class="metric"><b>${fmt(d.vehicle_current_a)}</b> A vehicle</span>
          <span class="metric"><b>${fmt(d.currentA_a)}</b>/<b>${fmt(d.currentB_a)}</b>/<b>${fmt(d.currentC_a)}</b> A</span>
          <span class="metric"><b>${fmt(d.session_energy_wh)}</b> Wh session</span>
        </div>
      </div>
      <div class="tag ${d.online === false ? 'offline' : ''}"><span class="dot"></span>${d.online === false ? 'Offline' : (d.contactor_closed ? 'Charging' : (d.vehicle_connected ? 'Connected' : 'Online'))}</div>
    </div>`).join('');
}

function fmt(v) {
  if (v === undefined || v === null) return '-';
  if (typeof v === 'number') return Math.round(v * 10) / 10;
  return v;
}

function pushNeurioHistory(values) {
  const now = Date.now();
  neurioHistory.push({ t: now, v: values.map(value => Number(value || 0)) });
  const cutoff = now - 60 * 60 * 1000;
  neurioHistory = neurioHistory.filter(point => point.t >= cutoff);
}

function drawSparkline(canvas, phaseIndex, maxAmp) {
  const rect = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * scale));
  const height = Math.max(1, Math.round(rect.height * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = '#e2e3e3';
  ctx.lineWidth = Math.max(1, scale);
  ctx.beginPath();
  ctx.moveTo(0, height - 1);
  ctx.lineTo(width, height - 1);
  ctx.stroke();
  if (neurioHistory.length < 2) return;
  const now = Date.now();
  const start = now - 60 * 60 * 1000;
  ctx.strokeStyle = phaseIndex === 0 ? '#171a20' : (phaseIndex === 1 ? '#e34f45' : '#3e6ae1');
  ctx.lineWidth = Math.max(2, 2 * scale);
  ctx.beginPath();
  neurioHistory.forEach((point, index) => {
    const x = Math.max(0, Math.min(width, ((point.t - start) / (60 * 60 * 1000)) * width));
    const y = height - Math.max(0, Math.min(1, point.v[phaseIndex] / maxAmp)) * (height - 4) - 2;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function renderHistory(values) {
  pushNeurioHistory(values);
  const peak = Math.max(16, 32, ...neurioHistory.flatMap(point => point.v));
  const maxAmp = Math.ceil(peak / 8) * 8;
  historyScale.textContent = `0-${maxAmp} A`;
  values.forEach((value, index) => {
    sparkValues[index].textContent = `${fmt(value)} A`;
    drawSparkline(sparkCanvases[index], index, maxAmp);
  });
}

function renderNeurio(data) {
  const values = (data.current_a || [0, 0, 0]).slice(0, 3).map(value => Number(value || 0));
  const powers = (data.power_w || []).slice(0, 3).map((value, index) => Number(value || values[index] * 230));
  if (!editingNeurio) {
    neurioMode.value = data.mode || 'manual';
    values.forEach((value, index) => { phaseInputs[index].value = fmt(value); });
  }
  const mode = neurioMode.value;
  phaseInputs.forEach(input => { input.disabled = mode !== 'manual'; });
  saveNeurioBtn.disabled = mode !== 'manual';
  const total = values.reduce((sum, value) => sum + Number(value || 0), 0);
  const totalPower = powers.reduce((sum, value) => sum + Number(value || 0), 0);
  neurioTotal.textContent = `${fmt(total)} A totalt · ${fmt(totalPower)} W`;
  values.forEach((value, index) => {
    phaseFills[index].style.width = `${Math.min(100, Math.max(0, Number(value || 0) / 32 * 100))}%`;
    wireAmps[index].textContent = `${fmt(value)} A`;
    wireWatts[index].textContent = `${fmt(powers[index])} W`;
  });
  renderHistory(values);
}

async function refreshNeurio(silent = true) {
  try {
    const res = await fetch(`/api/neurio?_=${Date.now()}`, { cache: 'no-store' });
    const data = await res.json();
    renderNeurio(data);
    if (!silent) log(`Neurio: ${data.current_a.slice(0,3).map(fmt).join('/')} A (${data.mode || 'manual'})`);
  } catch (err) {
    if (!silent) log(`FEL vid Neurio-uppdatering: ${err.message}`);
  }
}

async function saveNeurio() {
  const values = phaseInputs.map(input => Number(input.value));
  if (values.some(value => !Number.isFinite(value) || value < 0)) {
    log('FEL: ange giltiga amperevärden.');
    return;
  }
  saveNeurioBtn.disabled = true;
  try {
    const res = await fetch('/api/neurio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({ mode: neurioMode.value, current_a: values })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'kunde inte spara');
    renderNeurio(data);
    log(`Satte Neurio till ${values.map(fmt).join('/')} A`);
  } catch (err) {
    log(`FEL vid sparning av Neurio: ${err.message}`);
  } finally {
    saveNeurioBtn.disabled = neurioMode.value !== 'manual';
  }
}

async function refreshDevices(silent = false) {
  if (!silent) scanState.textContent = 'refreshing';
  try {
    const res = await fetch(`/api/devices?_=${Date.now()}`, { cache: 'no-store' });
    const data = await res.json();
    renderDevices(data.devices || []);
    startAutoRefresh();
    if (!silent) log(`Uppdaterade ${data.devices.length} kända laddboxar på ${data.duration_s}s`);
  } catch (err) {
    if (!silent) log(`FEL vid statusuppdatering: ${err.message}`);
  } finally {
    if (!silent) scanState.textContent = 'idle';
  }
}

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refreshDevices(true), 3000);
}

async function scan() {
  scanBtn.disabled = true;
  scanState.textContent = 'scanning';
  debugLog.textContent = 'Startar scan...';
  const subnet = subnetInput.value.trim();
  const url = subnet ? `/api/scan?subnet=${encodeURIComponent(subnet)}` : '/api/scan';
  try {
    const res = await fetch(`${url}${url.includes('?') ? '&' : '?'}_=${Date.now()}`, { cache: 'no-store' });
    const data = await res.json();
    log(`Subnet: ${data.subnet}`);
    log(`Testade ${data.hosts_scanned} adresser på ${data.duration_s}s`);
    for (const line of data.log) log(line);
    renderDevices(data.devices || []);
    startAutoRefresh();
  } catch (err) {
    log(`FEL: ${err.message}`);
  } finally {
    scanBtn.disabled = false;
    scanState.textContent = 'idle';
  }
}
scanBtn.addEventListener('click', scan);
refreshBtn.addEventListener('click', () => { subnetInput.value = ''; log('Subnet satt till auto.'); });
phaseInputs.forEach(input => {
  input.addEventListener('focus', () => { editingNeurio = true; });
  input.addEventListener('blur', () => { editingNeurio = false; refreshNeurio(true); });
  input.addEventListener('keydown', event => { if (event.key === 'Enter') saveNeurio(); });
});
neurioMode.addEventListener('change', () => { saveNeurio(); });
saveNeurioBtn.addEventListener('click', saveNeurio);
refreshDevices(false);
refreshNeurio(false);
startAutoRefresh();
neurioTimer = setInterval(() => refreshNeurio(true), 1000);
</script>
</body>
</html>
'''


def get_default_subnet() -> str:
    """Infer the primary IPv4 /24 to scan.

    Instead of hard-coding a site-specific LAN, inspect the default route
    interface and scan its local /24.  If a larger prefix is configured,
    deliberately narrow to /24 so a button press does not scan a huge
    corporate/private network.
    """
    try:
        route = subprocess.check_output(["ip", "-o", "-4", "route", "show", "to", "default"], text=True)
        iface = route.split(" dev ", 1)[1].split()[0]
        addr_lines = subprocess.check_output(["ip", "-o", "-4", "addr", "show", "dev", iface], text=True)
        cidr = addr_lines.split()[3]
        network = ipaddress.ip_interface(cidr).network
        if network.prefixlen < 24:
            ip = ipaddress.ip_interface(cidr).ip
            network = ipaddress.ip_network(f"{ip}/24", strict=False)
        return str(network)
    except Exception:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))


def fetch_json(ip: str, path: str):
    """Fetch and decode one Wall Connector API endpoint."""
    url = f"http://{ip}{path}"
    req = Request(url, headers={"User-Agent": "twc-neurio-control/0.1"})
    with urlopen(req, timeout=SCAN_TIMEOUT) as resp:
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read(65536)
    try:
        return json.loads(body.decode("utf-8", errors="replace")), ctype
    except json.JSONDecodeError:
        return None, ctype


def probe_host(ip: str):
    """Return a Wall Connector summary if an IP exposes the expected API.

    Detection is intentionally conservative: /api/1/vitals must return JSON
    containing evse_state.  Generic web servers on the LAN are ignored.
    """
    logs = []
    try:
        vitals, _ = fetch_json(ip, "/api/1/vitals")
        if not isinstance(vitals, dict) or "evse_state" not in vitals:
            return None, logs, "not a Wall Connector"
        version = None
        serial = None
        try:
            version_data, _ = fetch_json(ip, "/api/1/version")
            if isinstance(version_data, dict):
                version = version_data.get("firmware_version") or version_data.get("version")
                serial = version_data.get("serial_number") or version_data.get("part_number")
        except Exception:
            pass
        device = {"ip": ip, "version": version, "serial": serial}
        for key in [
            "contactor_closed", "vehicle_connected", "vehicle_current_a",
            "currentA_a", "currentB_a", "currentC_a", "session_energy_wh", "evse_state",
        ]:
            if key in vitals:
                device[key] = vitals[key]
        device['online'] = True
        device['checked_at'] = iso_now()
        device['last_seen_at'] = device['checked_at']
        logs.append(f"Hittade Wall Connector på {ip}")
        return device, logs, None
    except (URLError, HTTPError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
        return None, logs, f"{type(exc).__name__}: {exc}"


def load_neurio_values():
    """Read the simulator values file and normalize its shape for the UI."""
    try:
        with open(VALUES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    raw_current = data.get("current_a") if isinstance(data.get("current_a"), list) else [0.0, 0.0, 0.0, 0.0]
    current = []
    for index in range(4):
        try:
            current.append(float(raw_current[index]) if index < len(raw_current) else 0.0)
        except (TypeError, ValueError):
            current.append(0.0)
    current[3] = sum(current[:3])
    raw_power = data.get("power_w") if isinstance(data.get("power_w"), list) else []
    if len(raw_power) >= 5:
        power = raw_power
    else:
        power = [current[0] * DEFAULT_VOLTAGE, current[1] * DEFAULT_VOLTAGE, current[2] * DEFAULT_VOLTAGE, current[3] * DEFAULT_VOLTAGE, 0.0]
    return {
        "mode": data.get("mode", "manual"),
        "current_a": current,
        "power_w": power,
        "updated_at": data.get("updated_at"),
    }


def save_neurio_values(payload):
    """Validate and atomically write manual Neurio values from the UI.

    The serial simulator watches the same file, so no service restart is needed
    when a user changes L1/L2/L3 from the browser.
    """
    mode = payload.get("mode", "manual")
    if mode not in {"manual", "auto"}:
        raise ValueError("mode must be manual or auto")
    current_in = payload.get("current_a", [])
    if not isinstance(current_in, list) or len(current_in) < 3:
        raise ValueError("current_a must contain L1, L2 and L3")
    phases = [max(0.0, float(current_in[i])) for i in range(3)]
    total = sum(phases)
    data = {
        "mode": mode,
        "current_a": [phases[0], phases[1], phases[2], total],
        "power_w": [phases[0] * DEFAULT_VOLTAGE, phases[1] * DEFAULT_VOLTAGE, phases[2] * DEFAULT_VOLTAGE, total * DEFAULT_VOLTAGE, 0.0],
        "updated_at": iso_now(),
    }
    tmp_path = VALUES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    Path(tmp_path).replace(VALUES_PATH)
    return data


def load_known_devices():
    """Load persisted Wall Connector IP/serial hints."""
    try:
        with open(KNOWN_DEVICES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_known_devices(devices):
    """Persist discovered Wall Connectors without volatile online/status data."""
    try:
        by_serial_or_ip = {}
        for dev in load_known_devices() + devices:
            key = dev.get("serial") or dev.get("ip")
            if not key:
                continue
            keep = dict(dev)
            keep.pop("online", None)
            by_serial_or_ip[key] = keep
        with open(KNOWN_DEVICES_PATH, "w", encoding="utf-8") as f:
            json.dump(list(by_serial_or_ip.values()), f, indent=2)
    except Exception as exc:
        print(f"Could not save known devices: {exc}")


def merge_known_devices(found):
    """Combine scan results with previously known devices.

    This prevents a charger with temporary Wi-Fi trouble from disappearing from
    the UI; instead it remains visible as offline with the last known identity.
    """
    save_known_devices(found)
    merged = []
    seen = set()
    for dev in found:
        key = dev.get("serial") or dev.get("ip")
        seen.add(key)
        merged.append(dev)
    for dev in load_known_devices():
        key = dev.get("serial") or dev.get("ip")
        if key in seen:
            continue
        offline = dict(dev)
        offline["online"] = False
        merged.append(offline)
    return merged


def refresh_known_devices():
    """Poll only known Wall Connector IPs for fast live status updates."""
    started = time.time()
    refreshed = []
    logs = []
    for known in load_known_devices():
        ip = known.get("ip")
        if not ip:
            continue
        device, host_logs, error = probe_host(ip)
        logs.extend(host_logs)
        if device:
            merged = dict(known)
            merged.update(device)
            merged["online"] = True
            merged["checked_at"] = device.get("checked_at") or iso_now()
            merged["last_seen_at"] = device.get("last_seen_at") or merged["checked_at"]
            refreshed.append(merged)
        else:
            offline = dict(known)
            offline["online"] = False
            offline["checked_at"] = iso_now()
            offline["status_error"] = error or "no response"
            refreshed.append(offline)
    refreshed.sort(key=lambda d: tuple(int(part) for part in d["ip"].split(".")))
    return {
        "duration_s": round(time.time() - started, 2),
        "devices": refreshed,
        "log": logs,
    }


def scan_subnet(subnet: str):
    """Scan every host in a subnet for Wall Connector APIs."""
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(ip) for ip in network.hosts()]
    started = time.time()
    devices = []
    logs = [f"Skannar {network} ({len(hosts)} hosts)"]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(probe_host, ip): ip for ip in hosts}
        for future in as_completed(futures):
            device, host_logs, error = future.result()
            logs.extend(host_logs)
            if device:
                devices.append(device)
    devices = merge_known_devices(devices)
    devices.sort(key=lambda d: tuple(int(part) for part in d["ip"].split(".")))
    return {
        "subnet": str(network),
        "hosts_scanned": len(hosts),
        "duration_s": round(time.time() - started, 2),
        "devices": devices,
        "log": logs,
    }


class Handler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for static HTML plus a few JSON endpoints."""
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, status: int, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/neurio":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8"))
                self.send_json(200, save_neurio_values(payload))
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return
        self.send_error(404)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/scan":
            subnet = None
            for part in parsed.query.split("&"):
                if part.startswith("subnet="):
                    subnet = part.split("=", 1)[1].replace("%2F", "/")
            try:
                result = scan_subnet(subnet or get_default_subnet())
                self.send_json(200, result)
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/devices":
            try:
                self.send_json(200, refresh_known_devices())
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/neurio":
            self.send_json(200, load_neurio_values())
            return
        if parsed.path == "/api/status":
            self.send_json(200, {"ok": True, "default_subnet": get_default_subnet()})
            return
        self.send_error(404)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
