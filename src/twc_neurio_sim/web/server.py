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
IDENTIFY_PATH = "/etc/twc-neurio-sim/identify.json"
FRONIUS_CONFIG_PATH = "/etc/twc-neurio-sim/fronius.json"
CONTROL_CONFIG_PATH = "/etc/twc-neurio-sim/control.json"
CONTROL_STATE_PATH = "/run/twc-neurio-sim/control_state.json"
PORT_ACTIVITY_PATH = "/run/twc-neurio-sim/port_activity.json"
DEFAULT_VOLTAGE = 230.0
DEFAULT_IDENTIFY_DURATION_S = 8
MOXA_MODEL = "Moxa UPort 1650-16"
MOXA_PORT_COUNT = 16
PORT_ACTIVE_WINDOW_S = 10
SERIAL_CONFIG_CACHE_S = 5
SERIAL_INTERFACE_LABELS = {
    0x0: "RS232",
    0x1: "RS485 2-wire",
    0x2: "RS422",
    0x3: "RS485 4-wire",
}
_serial_config_cache = {"expires_at": 0.0, "ports": {}}
DEFAULT_CONTROL_CONFIG = {
    "main_fuse_a": 25.0,
    "slow_overload_pct": 100.0,
    "medium_overload_pct": 110.0,
    "fast_overload_pct": 120.0,
    "slow_response_s": 600.0,
    "medium_response_s": 10.0,
    "fast_response_s": 1.0,
    "recovery_s": 120.0,
    "noise_floor_a": 0.3,
}

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
    .fronius-panel { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 16px; }
    .fronius-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .fronius-title { font-size: 15px; font-weight: 800; }
    .fronius-toggle { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-weight: 700; font-size: 13px; }
    .fronius-toggle input { width: 18px; height: 18px; accent-color: var(--blue); }
    .fronius-grid { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; }
    .fronius-ip { height: 38px; border: 1px solid var(--line); border-radius: 6px; padding: 0 10px; font: inherit; background: #fff; min-width: 0; }
    .fronius-status { grid-column: 1 / -1; color: var(--muted); font-size: 13px; min-height: 19px; }
    .fronius-status.ok { color: #18820d; }
    .fronius-status.bad { color: var(--danger); }
    .fronius-readings { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 4px; }
    .fronius-reading { background: var(--panel-soft); border-radius: 6px; padding: 8px; font-size: 12px; color: var(--muted); }
    .fronius-reading b { display: block; color: var(--text); font-size: 15px; margin-bottom: 2px; }
    .control-panel { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 16px; }
    .control-title { font-size: 15px; font-weight: 800; margin-bottom: 10px; }
    .control-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .control-field { display: grid; gap: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
    .control-field input { height: 36px; border: 1px solid var(--line); border-radius: 6px; padding: 0 9px; font: inherit; background: #fff; min-width: 0; }
    .control-wide { grid-column: 1 / -1; }
    .control-status { grid-column: 1 / -1; color: var(--muted); font-size: 13px; min-height: 19px; }
    .control-status.ok { color: #18820d; }
    .control-status.warn { color: #a35a00; }
    .control-status.bad { color: var(--danger); }
    .control-metrics { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 4px; }
    .control-metric { background: var(--panel-soft); border-radius: 6px; padding: 8px; font-size: 12px; color: var(--muted); }
    .control-metric b { display: block; color: var(--text); font-size: 15px; margin-bottom: 2px; }
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
    .row.empty-port { grid-template-columns: 1fr auto; padding: 10px 18px; background: #f1f1ef; color: var(--muted); }
    .row.empty-port .wc-name { color: var(--muted); font-size: 14px; font-weight: 500; }
    .row.empty-port .port-badge { height: 22px; margin-bottom: 2px; font-size: 12px; font-weight: 600; }
    .row.rs485-active { background: #f7fbf5; }
    .wc-name { font-size: 19px; font-weight: 700; }
    .wc-meta { color: var(--muted); margin-top: 3px; }
    .port-line { color: var(--text); font-weight: 700; margin-top: 4px; }
    .empty-port .port-line { color: var(--muted); font-size: 12px; font-weight: 400; margin-top: 1px; }
    .device-name-input { width: min(260px, 100%); height: 34px; margin-top: 8px; border: 1px solid var(--line); border-radius: 6px; padding: 0 9px; font: inherit; font-size: 14px; background: #fff; }
    .port-activity { color: var(--muted); font-size: 12px; margin-top: 5px; }
    .port-config { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .port-config.bad { color: var(--danger); font-weight: 700; }
    .metric-strip { display: flex; gap: 18px; color: var(--muted); margin-top: 9px; flex-wrap: wrap; }
    .metric b { color: var(--text); }
    .port-badge { display: inline-flex; align-items: center; height: 28px; padding: 0 10px; border-radius: 999px; background: #f1f1ef; color: var(--muted); font-size: 13px; font-weight: 800; margin-bottom: 6px; }
    .tag { display: inline-flex; align-items: center; gap: 8px; height: 34px; padding: 0 12px; border-radius: 999px; background: #eff8ed; color: #18820d; font-weight: 700; }
    .tag.offline { background: #f4eeee; color: #9b1c12; }
    .tag.offline .dot { background: var(--danger); box-shadow: 0 0 0 4px rgba(217,48,37,.12); }
    .tag.empty { height: 28px; background: #e4e4e2; color: #777b82; font-size: 13px; font-weight: 600; }
    .tag.empty .dot { background: #a9abad; box-shadow: none; }
    .tag.rs485 { background: #eff8ed; color: #18820d; }
    .tag.rs485 .dot { background: var(--green); box-shadow: 0 0 0 4px rgba(18,189,0,.13); }
    .tag.stale { background: #eeeeec; color: #6f7378; }
    .tag.stale .dot { background: #a9abad; box-shadow: none; }
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
              <option value="fronius">Fronius</option>
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
          <div class="fronius-panel">
            <div class="fronius-head">
              <div class="fronius-title">Fronius Smart Meter</div>
              <label class="fronius-toggle"><input id="froniusEnabled" type="checkbox" /> Aktiv</label>
            </div>
            <div class="fronius-grid">
              <input id="froniusIp" class="fronius-ip" placeholder="Fronius IP, t.ex. 192.0.2.20" inputmode="decimal" aria-label="Fronius IP address" />
              <button id="saveFroniusBtn" class="save-small">Spara</button>
              <div id="froniusStatus" class="fronius-status">Avstängd</div>
              <div class="fronius-readings">
                <div class="fronius-reading"><b id="froniusA1">- A</b><span id="froniusP1">- W</span></div>
                <div class="fronius-reading"><b id="froniusA2">- A</b><span id="froniusP2">- W</span></div>
                <div class="fronius-reading"><b id="froniusA3">- A</b><span id="froniusP3">- W</span></div>
              </div>
            </div>
          </div>
          <div class="control-panel">
            <div class="control-title">Automatisk lastreglering</div>
            <div class="control-grid">
              <label class="control-field">Huvudsäkring (A)<input id="mainFuseA" type="number" min="1" max="400" step="1" /></label>
              <button id="saveControlBtn" class="save-small">Spara</button>
              <label class="control-field">Långsam gräns (%)<input id="slowPct" type="number" min="50" max="200" step="0.1" /></label>
              <label class="control-field">Långsam tid (s)<input id="slowSeconds" type="number" min="1" max="7200" step="1" /></label>
              <label class="control-field">Mellan gräns (%)<input id="mediumPct" type="number" min="50" max="250" step="0.1" /></label>
              <label class="control-field">Mellan tid (s)<input id="mediumSeconds" type="number" min="1" max="600" step="1" /></label>
              <label class="control-field">Snabb gräns (%)<input id="fastPct" type="number" min="50" max="300" step="0.1" /></label>
              <label class="control-field">Snabb tid (s)<input id="fastSeconds" type="number" min="0.2" max="60" step="0.1" /></label>
              <label class="control-field control-wide">Återgångstid (s)<input id="recoverySeconds" type="number" min="1" max="1800" step="1" /></label>
              <div id="controlStatus" class="control-status">Auto-läge ej aktivt</div>
              <div class="control-metrics">
                <div class="control-metric"><b id="controlLoadPct">- %</b><span>högsta fas</span></div>
                <div class="control-metric"><b id="controlBaseLoad">- A</b><span>huslast utan laddning</span></div>
                <div class="control-metric"><b id="controlPenalty">- A</b><span>regulatorpådrag</span></div>
              </div>
            </div>
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
const froniusEnabled = document.getElementById('froniusEnabled');
const froniusIp = document.getElementById('froniusIp');
const saveFroniusBtn = document.getElementById('saveFroniusBtn');
const froniusStatus = document.getElementById('froniusStatus');
const froniusAmps = [document.getElementById('froniusA1'), document.getElementById('froniusA2'), document.getElementById('froniusA3')];
const froniusPowers = [document.getElementById('froniusP1'), document.getElementById('froniusP2'), document.getElementById('froniusP3')];
const saveControlBtn = document.getElementById('saveControlBtn');
const controlStatus = document.getElementById('controlStatus');
const controlInputs = {
  main_fuse_a: document.getElementById('mainFuseA'),
  slow_overload_pct: document.getElementById('slowPct'),
  slow_response_s: document.getElementById('slowSeconds'),
  medium_overload_pct: document.getElementById('mediumPct'),
  medium_response_s: document.getElementById('mediumSeconds'),
  fast_overload_pct: document.getElementById('fastPct'),
  fast_response_s: document.getElementById('fastSeconds'),
  recovery_s: document.getElementById('recoverySeconds')
};
const controlLoadPct = document.getElementById('controlLoadPct');
const controlBaseLoad = document.getElementById('controlBaseLoad');
const controlPenalty = document.getElementById('controlPenalty');
let refreshTimer = null;
let neurioTimer = null;
let froniusTimer = null;
let controlTimer = null;
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

function activityText(slot) {
  const activity = slot.activity || {};
  const bits = [];
  if (slot.identity_serial) bits.push(`Neurio ${esc(slot.identity_serial)}`);
  if (activity.request_count) bits.push(`${activity.request_count} Modbus-frågor`);
  const seen = fmtTime(activity.last_request_at_ms);
  if (seen) bits.push(`senast ${seen}`);
  if (activity.identity_read_count) bits.push(`identity ${activity.identity_read_count}x: ${esc(activity.last_identity_serial || '-')}`);
  return bits.length ? `<div class="port-activity">${bits.join(' · ')}</div>` : '';
}

function serialConfigText(slot) {
  const config = slot.serial_config || {};
  const text = config.text || 'Okänd';
  return `<div class="port-config ${config.ok === false ? 'bad' : ''}">Aktuell konfiguration: ${esc(text)}</div>`;
}

function deviceStatusText(d, active = true) {
  if (!d || (!d.occupied && !d.ip)) return 'Tom port';
  if (d.online === false) return 'Offline';
  if (!active) return 'Ingen RS485';
  if (d.contactor_closed) return 'Charging';
  if (d.vehicle_connected) return 'Connected';
  return 'Online';
}

function deviceStatusClass(d, active = true) {
  if (!d || (!d.occupied && !d.ip)) return 'empty';
  if (d.online !== false && !active) return 'stale';
  return d.online === false ? 'offline' : '';
}

function renderDevices(portsOrDevices) {
  lastDevices = portsOrDevices || [];
  if (!lastDevices.length) {
    results.innerHTML = '<div class="empty">Inga Moxa-portar kunde visas ännu.</div>';
    return;
  }
  results.innerHTML = lastDevices.map(slot => {
    const d = slot.device || slot;
    const occupied = Boolean(slot.occupied ?? d.ip);
    const portLabel = slot.moxa_label || d.moxa_label || (slot.moxa_port ? `Moxa UPort 1650-16:Port${slot.moxa_port}` : 'Moxa UPort 1650-16');
    const tty = slot.tty || d.tty || (slot.moxa_port ? `/dev/ttyMXUSB${slot.moxa_port - 1}` : '');
    const active = Boolean(slot.rs485_active);
    const rowClasses = ['row'];
    if (!occupied) rowClasses.push('empty-port');
    if (active) rowClasses.push('rs485-active');
    if (!occupied) {
      return `
    <div class="${rowClasses.join(' ')}">
      <div>
        <div class="port-badge">Port ${slot.moxa_port}</div>
        <div class="wc-name">${active ? 'RS485-trafik utan mappad laddbox' : 'Ingen laddbox ansluten'}</div>
        <div class="port-line">${esc(portLabel)}${tty ? ' · ' + esc(tty) : ''}</div>
        ${serialConfigText(slot)}
        ${activityText(slot)}
      </div>
      <div class="tag ${active ? 'rs485' : 'empty'}"><span class="dot"></span>${active ? 'RS485 aktiv' : 'Tom port'}</div>
    </div>`;
    }
    const displayName = d.display_name || '';
    const displayTitle = displayName ? esc(displayName) : 'Wall Connector';
    const portConnectionText = active ? `ansluten till ${esc(portLabel)}` : `${esc(portLabel)} · ingen RS485-trafik`;
    return `
    <div class="${rowClasses.join(' ')}">
      <div>
        <div class="port-badge">Port ${d.moxa_port || slot.moxa_port || '-'}</div>
        <div class="wc-name">${displayTitle}${d.serial ? ' · ' + esc(d.serial) : ''}</div>
        <div class="port-line">${portConnectionText}</div>
        ${serialConfigText(slot)}
        <div class="wc-meta">${esc(d.ip)}${d.version ? ' · firmware ' + esc(d.version) : ''}${d.serial ? ' · ' + esc(d.serial) : ''}</div>
        ${statusMeta(d)}
        ${activityText(slot)}
        <input class="device-name-input" data-device-key="${esc(d.serial || d.ip || '')}" value="${esc(displayName)}" placeholder="Eget namn, t.ex. Gården 2" />
        <div class="metric-strip">
          <span class="metric"><b>${fmt(d.vehicle_current_a)}</b> A vehicle</span>
          <span class="metric"><b>${fmt(d.currentA_a)}</b>/<b>${fmt(d.currentB_a)}</b>/<b>${fmt(d.currentC_a)}</b> A</span>
          <span class="metric"><b>${fmt(d.session_energy_wh)}</b> Wh session</span>
        </div>
      </div>
      <div class="tag ${deviceStatusClass(d, active)}"><span class="dot"></span>${deviceStatusText(d, active)}</div>
    </div>`;
  }).join('');
  bindDeviceNameInputs();
}

function bindDeviceNameInputs() {
  document.querySelectorAll('.device-name-input').forEach(input => {
    input.addEventListener('keydown', event => { if (event.key === 'Enter') input.blur(); });
    input.addEventListener('change', () => saveDeviceName(input));
  });
}

async function saveDeviceName(input) {
  const key = input.dataset.deviceKey;
  if (!key) return;
  try {
    const res = await fetch('/api/device-name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({ key, display_name: input.value.trim() })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'kunde inte spara namn');
    renderDevices(data.ports || data.devices || []);
    log(`Sparade namn för ${key}`);
  } catch (err) {
    log(`FEL vid namnsparning: ${err.message}`);
  }
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

function renderFronius(data) {
  froniusEnabled.checked = Boolean(data.enabled);
  froniusIp.value = data.ip || '';
  const meter = data.meter || null;
  froniusStatus.classList.remove('ok', 'bad');
  if (!data.enabled) {
    froniusStatus.textContent = 'Avstängd';
  } else if (data.error) {
    froniusStatus.textContent = `Fel: ${data.error}`;
    froniusStatus.classList.add('bad');
  } else if (meter) {
    froniusStatus.textContent = `${meter.model || 'Smart Meter'} · ${meter.timestamp || 'live'} · skriver till Neurio`;
    froniusStatus.classList.add('ok');
  } else {
    froniusStatus.textContent = 'Aktiv, väntar på data';
  }
  const currents = (meter?.current_a || [null, null, null]).slice(0, 3);
  const powers = (meter?.power_w || [null, null, null]).slice(0, 3);
  currents.forEach((value, index) => { if (froniusAmps[index]) froniusAmps[index].textContent = `${fmt(value)} A`; });
  powers.forEach((value, index) => { if (froniusPowers[index]) froniusPowers[index].textContent = `${fmt(value)} W`; });
}

function renderControl(data) {
  const cfg = data.config || {};
  Object.entries(controlInputs).forEach(([key, input]) => {
    if (document.activeElement !== input && cfg[key] !== undefined) input.value = fmt(Number(cfg[key]));
  });
  const state = data.state || {};
  controlStatus.classList.remove('ok', 'warn', 'bad');
  if (state.error) {
    controlStatus.textContent = `Fel: ${state.error}`;
    controlStatus.classList.add('bad');
  } else if (state.active) {
    const band = state.band || 'normal';
    controlStatus.textContent = `Aktiv · ${band} · skriver syntetisk Neurio-last`;
    controlStatus.classList.add(band === 'normal' ? 'ok' : 'warn');
  } else {
    controlStatus.textContent = state.reason || 'Auto-läge ej aktivt';
  }
  controlLoadPct.textContent = `${fmt(state.max_load_pct)} %`;
  controlBaseLoad.textContent = `${(state.base_load_a || []).slice(0,3).map(fmt).join('/')} A`;
  controlPenalty.textContent = `${(state.penalty_a || []).slice(0,3).map(fmt).join('/')} A`;
}

async function refreshControl(silent = true) {
  try {
    const res = await fetch(`/api/control?_=${Date.now()}`, { cache: 'no-store' });
    const data = await res.json();
    renderControl(data);
    if (!silent && data.state?.active) log(`Regulator: ${fmt(data.state.max_load_pct)}% · ${data.state.band}`);
  } catch (err) {
    if (!silent) log(`FEL vid regulatoruppdatering: ${err.message}`);
  }
}

async function saveControl() {
  saveControlBtn.disabled = true;
  const payload = {};
  Object.entries(controlInputs).forEach(([key, input]) => { payload[key] = Number(input.value); });
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'kunde inte spara regulatorinställningar');
    renderControl(data);
    log(`Sparade regulator: huvudsäkring ${fmt(data.config.main_fuse_a)} A`);
  } catch (err) {
    log(`FEL vid regulatorsparning: ${err.message}`);
  } finally {
    saveControlBtn.disabled = false;
  }
}

async function refreshFronius(silent = true) {
  try {
    const res = await fetch(`/api/fronius?_=${Date.now()}`, { cache: 'no-store' });
    const data = await res.json();
    renderFronius(data);
    if (!silent && data.enabled) log(`Fronius: ${data.meter ? data.meter.current_a.slice(0,3).map(fmt).join('/') + ' A' : data.error || 'ingen data'}`);
  } catch (err) {
    froniusStatus.textContent = `Fel: ${err.message}`;
    froniusStatus.classList.add('bad');
  }
}

async function saveFronius() {
  saveFroniusBtn.disabled = true;
  try {
    const res = await fetch('/api/fronius', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({ enabled: froniusEnabled.checked, ip: froniusIp.value.trim() })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'kunde inte spara Fronius-inställning');
    renderFronius(data);
    log(`Fronius ${data.enabled ? 'aktiverad' : 'avstängd'} på ${data.ip || '-'}`);
    refreshNeurio(true);
  } catch (err) {
    log(`FEL vid Fronius-sparning: ${err.message}`);
  } finally {
    saveFroniusBtn.disabled = false;
  }
}

async function refreshDevices(silent = false) {
  if (!silent) scanState.textContent = 'refreshing';
  try {
    const res = await fetch(`/api/devices?_=${Date.now()}`, { cache: 'no-store' });
    const data = await res.json();
    if (!document.activeElement?.classList?.contains('device-name-input')) {
      renderDevices(data.ports || data.devices || []);
    }
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
    renderDevices(data.ports || data.devices || []);
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
saveFroniusBtn.addEventListener('click', saveFronius);
froniusEnabled.addEventListener('change', saveFronius);
saveControlBtn.addEventListener('click', saveControl);
refreshDevices(false);
refreshNeurio(false);
refreshFronius(false);
refreshControl(false);
startAutoRefresh();
neurioTimer = setInterval(() => refreshNeurio(true), 1000);
froniusTimer = setInterval(() => refreshFronius(true), 1000);
controlTimer = setInterval(() => refreshControl(true), 2000);
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


def load_fronius_config():
    """Load optional Fronius Smart Meter integration settings."""
    try:
        with open(FRONIUS_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {"enabled": bool(data.get("enabled", False)), "ip": str(data.get("ip", "")).strip()}
    except Exception:
        pass
    return {"enabled": False, "ip": ""}


def save_fronius_config(payload):
    """Persist Fronius integration settings.

    The IP address is intentionally user-supplied.  The project must not ship
    with a site-specific private address baked into the public repository.
    """
    ip = str(payload.get("ip", "")).strip()
    enabled = bool(payload.get("enabled", False))
    if enabled and not ip:
        raise ValueError("Fronius IP address is required when enabled")
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError("Fronius IP address is invalid")
    data = {"enabled": enabled, "ip": ip, "updated_at": iso_now()}
    Path(FRONIUS_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = FRONIUS_CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    Path(tmp_path).replace(FRONIUS_CONFIG_PATH)
    return data


def load_control_config():
    """Load fuse/regulator settings used by Auto mode."""
    config = dict(DEFAULT_CONTROL_CONFIG)
    try:
        with open(CONTROL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in config:
                if key in data:
                    config[key] = float(data[key])
    except Exception:
        pass
    return config


def save_control_config(payload):
    """Persist regulator settings with bounded numeric values.

    Overload thresholds are exact comparisons against percentages.  Anything
    strictly greater than 100.0 % enters the slow overload band, so there is no
    dead zone between 100.001 % and 100.999 %.
    """
    current = load_control_config()
    for key in DEFAULT_CONTROL_CONFIG:
        if key in payload:
            current[key] = float(payload[key])
    if current["main_fuse_a"] <= 0:
        raise ValueError("main_fuse_a must be greater than zero")
    if not (0 < current["slow_overload_pct"] < current["medium_overload_pct"] < current["fast_overload_pct"]):
        raise ValueError("overload thresholds must be increasing")
    for key in ("slow_response_s", "medium_response_s", "fast_response_s", "recovery_s"):
        if current[key] <= 0:
            raise ValueError(f"{key} must be greater than zero")
    if current["noise_floor_a"] < 0:
        raise ValueError("noise_floor_a must not be negative")
    data = {**current, "updated_at": iso_now()}
    Path(CONTROL_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONTROL_CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    Path(tmp_path).replace(CONTROL_CONFIG_PATH)
    return data


def load_control_state():
    """Load the last regulator state so ramping survives HTTP requests."""
    try:
        with open(CONTROL_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_control_state(state):
    """Persist live regulator state under /run for UI/debug visibility."""
    try:
        Path(CONTROL_STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
        tmp_path = CONTROL_STATE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        Path(tmp_path).replace(CONTROL_STATE_PATH)
    except Exception as exc:
        print(f"Could not save control state: {exc}")


def fetch_fronius_meter(ip):
    """Read the Fronius Open Data Smart Meter endpoint.

    This uses Fronius Solar API v1:

        /solar_api/v1/GetMeterRealtimeData.cgi?Scope=System

    The returned current/power values can be written into the Neurio simulator
    as an impromptu current source when no physical Neurio meter is available.
    """
    url = f"http://{ip}/solar_api/v1/GetMeterRealtimeData.cgi?Scope=System"
    req = Request(url, headers={"User-Agent": "twc-neurio-control/0.1"})
    with urlopen(req, timeout=3.0) as resp:
        body = resp.read(262144)
    data = json.loads(body.decode("utf-8", errors="replace"))
    meters = data.get("Body", {}).get("Data", {})
    if not isinstance(meters, dict) or not meters:
        raise ValueError("No Fronius meter data returned")
    meter = None
    for candidate in meters.values():
        if isinstance(candidate, dict) and "Current_AC_Phase_1" in candidate:
            meter = candidate
            break
    if meter is None:
        raise ValueError("No meter with phase currents found")
    currents = [float(meter.get(f"Current_AC_Phase_{i}", 0.0) or 0.0) for i in (1, 2, 3)]
    powers = [float(meter.get(f"PowerReal_P_Phase_{i}", currents[i - 1] * DEFAULT_VOLTAGE) or 0.0) for i in (1, 2, 3)]
    total_current = float(meter.get("Current_AC_Sum", sum(currents)) or sum(currents))
    total_power = float(meter.get("PowerReal_P_Sum", sum(powers)) or sum(powers))
    details = meter.get("Details", {}) if isinstance(meter.get("Details"), dict) else {}
    return {
        "model": details.get("Model") or "Fronius Smart Meter",
        "manufacturer": details.get("Manufacturer") or "Fronius",
        "timestamp": data.get("Head", {}).get("Timestamp"),
        "current_a": [currents[0], currents[1], currents[2], total_current],
        "power_w": [powers[0], powers[1], powers[2], total_power, 0.0],
        "voltage_v": [meter.get("Voltage_AC_Phase_1"), meter.get("Voltage_AC_Phase_2"), meter.get("Voltage_AC_Phase_3")],
        "raw_power_sum_w": total_power,
    }


def write_neurio_values(data):
    """Atomically write the simulator values file used by the Modbus process."""
    tmp_path = VALUES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    Path(tmp_path).replace(VALUES_PATH)
    return data


def apply_fronius_to_neurio(meter):
    """Write Fronius Smart Meter readings to the Neurio simulator values file."""
    data = {
        "mode": "fronius",
        "source": "fronius",
        "current_a": meter["current_a"],
        "power_w": meter["power_w"],
        "updated_at": iso_now(),
    }
    return write_neurio_values(data)


def phase_currents_from_device(device):
    """Return measured Wall Connector output current per phase."""
    phases = []
    for key in ("currentA_a", "currentB_a", "currentC_a"):
        try:
            phases.append(max(0.0, float(device.get(key, 0.0) or 0.0)))
        except (TypeError, ValueError):
            phases.append(0.0)
    return phases


def poll_charger_currents():
    """Poll known Wall Connectors and sum their live phase currents."""
    known = [dev for dev in load_known_devices() if dev.get("ip")]
    totals = [0.0, 0.0, 0.0]
    devices = []
    if not known:
        return totals, devices
    with ThreadPoolExecutor(max_workers=min(8, len(known))) as executor:
        futures = {executor.submit(probe_host, dev["ip"]): dev for dev in known}
        for future in as_completed(futures):
            fallback = futures[future]
            device, _logs, _error = future.result()
            if not device:
                devices.append({"ip": fallback.get("ip"), "online": False})
                continue
            phases = phase_currents_from_device(device)
            for index in range(3):
                totals[index] += phases[index]
            devices.append({
                "ip": device.get("ip"),
                "serial": device.get("serial"),
                "online": True,
                "contactor_closed": device.get("contactor_closed"),
                "vehicle_connected": device.get("vehicle_connected"),
                "phase_current_a": phases,
            })
    return totals, devices


def response_time_for_load(load_pct, config):
    """Return overload band and requested response time for a load percentage."""
    if load_pct > config["fast_overload_pct"]:
        return "fast", config["fast_response_s"]
    if load_pct > config["medium_overload_pct"]:
        return "medium", config["medium_response_s"]
    if load_pct > config["slow_overload_pct"]:
        return "slow", config["slow_response_s"]
    return "normal", config["recovery_s"]


def ramp_value(current, target, dt_s, response_s):
    """Move one value toward a target using a first-order time constant."""
    if response_s <= 0:
        return target
    factor = max(0.0, min(1.0, dt_s / response_s))
    return current + (target - current) * factor


def apply_auto_control_to_neurio(meter):
    """Run the first practical anti-hunting regulator and write Neurio values."""
    config = load_control_config()
    now = time.time()
    previous = load_control_state()
    previous_time = float(previous.get("timestamp", now) or now)
    dt_s = max(0.1, min(30.0, now - previous_time))

    site_current = [max(0.0, float(value or 0.0)) for value in meter["current_a"][:3]]
    charger_current, chargers = poll_charger_currents()
    base_load = [max(0.0, site_current[i] - charger_current[i]) for i in range(3)]
    fuse = max(1.0, float(config["main_fuse_a"]))
    load_pct = [(site_current[i] / fuse) * 100.0 for i in range(3)]
    previous_penalty = previous.get("penalty_a") if isinstance(previous.get("penalty_a"), list) else [0.0, 0.0, 0.0]
    penalty = []
    bands = []

    for index in range(3):
        previous_value = float(previous_penalty[index] if index < len(previous_penalty) else 0.0)
        target = max(0.0, site_current[index] - fuse)
        if target < config["noise_floor_a"]:
            target = 0.0
        band, response_s = response_time_for_load(load_pct[index], config)
        if target <= previous_value:
            response_s = config["recovery_s"]
        penalty.append(ramp_value(previous_value, target, dt_s, response_s))
        bands.append(band)

    synthetic = [base_load[i] + penalty[i] for i in range(3)]
    total = sum(synthetic)
    state = {
        "active": True,
        "timestamp": now,
        "updated_at": iso_now(),
        "band": max(bands, key=lambda name: {"normal": 0, "slow": 1, "medium": 2, "fast": 3}[name]),
        "main_fuse_a": fuse,
        "site_current_a": site_current,
        "charger_current_a": charger_current,
        "base_load_a": base_load,
        "penalty_a": penalty,
        "synthetic_current_a": [synthetic[0], synthetic[1], synthetic[2], total],
        "max_load_pct": max(load_pct),
        "load_pct": load_pct,
        "chargers": chargers,
    }
    save_control_state(state)
    return write_neurio_values({
        "mode": "auto",
        "source": "fronius-auto",
        "current_a": state["synthetic_current_a"],
        "power_w": [
            synthetic[0] * DEFAULT_VOLTAGE,
            synthetic[1] * DEFAULT_VOLTAGE,
            synthetic[2] * DEFAULT_VOLTAGE,
            total * DEFAULT_VOLTAGE,
            0.0,
        ],
        "controller": state,
        "updated_at": iso_now(),
    })


def control_status():
    """Return saved control config and most recent regulator state."""
    config = load_control_config()
    mode = load_neurio_values().get("mode", "manual")
    state = load_control_state()
    if mode != "auto":
        state = {"active": False, "reason": "Välj Auto-läge för att aktivera regulatorn", "mode": mode}
    elif not state:
        state = {"active": False, "reason": "Auto-läge väntar på Fronius-data", "mode": mode}
    else:
        state["mode"] = mode
    return {"config": config, "state": state}


def get_fronius_status(update_neurio=True):
    """Return Fronius status and optionally feed live values to Neurio."""
    config = load_fronius_config()
    result = {"enabled": config["enabled"], "ip": config["ip"], "meter": None, "error": None}
    if not config["enabled"]:
        return result
    if not config["ip"]:
        result["error"] = "Fronius IP address is not configured"
        return result
    try:
        meter = fetch_fronius_meter(config["ip"])
        result["meter"] = meter
        if update_neurio:
            mode = load_neurio_values().get("mode", "manual")
            if mode == "auto":
                apply_auto_control_to_neurio(meter)
                result["control"] = control_status()["state"]
            elif mode == "fronius":
                apply_fronius_to_neurio(meter)
    except Exception as exc:
        result["error"] = str(exc)
    return result


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
    if mode not in {"manual", "auto", "fronius"}:
        raise ValueError("mode must be manual, auto or fronius")
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


def identify_signature_for_port(port_number):
    """Return a deliberately recognizable per-port current signature."""
    current = round(port_number + port_number / 100.0, 2)
    total = current * 3
    return {
        "current_a": [current, current, current, total],
        "power_w": [current * DEFAULT_VOLTAGE, current * DEFAULT_VOLTAGE, current * DEFAULT_VOLTAGE, total * DEFAULT_VOLTAGE, 0.0],
    }


def start_identify_signatures(payload):
    """Create short-lived per-port Neurio load signatures.

    This is the safe half of future autodetection: the simulator can emit a
    known value per RS485 port.  Matching that value back to a Wall Connector
    still requires an observable API field or a manual/Tesla One observation.
    """
    duration_s = float(payload.get("duration_s", DEFAULT_IDENTIFY_DURATION_S))
    duration_s = max(1.0, min(duration_s, 60.0))
    ports_in = payload.get("ports")
    if ports_in is None:
        ports = list(range(1, MOXA_PORT_COUNT + 1))
    elif isinstance(ports_in, list):
        ports = [port for port in (normalized_moxa_port(item) for item in ports_in) if port is not None]
    else:
        raise ValueError("ports must be a list of Moxa port numbers")
    if not ports:
        raise ValueError("at least one valid port is required")

    expires_at = time.time() + duration_s
    data = {
        "mode": "identify",
        "duration_s": duration_s,
        "expires_at": expires_at,
        "ports": {},
        "updated_at": iso_now(),
    }
    for port_number in ports:
        signature = identify_signature_for_port(port_number)
        data["ports"][str(port_number)] = {
            "expires_at": expires_at,
            **signature,
        }

    Path(IDENTIFY_PATH).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = IDENTIFY_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    Path(tmp_path).replace(IDENTIFY_PATH)
    return data


def moxa_tty(port_number):
    """Return the Linux device name for a 1-based Moxa physical port."""
    return f"/dev/ttyMXUSB{port_number - 1}"


def moxa_label(port_number):
    """Return the human-readable Moxa port label shown in the UI."""
    return f"{MOXA_MODEL}:Port{port_number}"


def parse_setserial_line(line):
    """Parse one `setserial -g` line into interface metadata."""
    path = line.split(",", 1)[0].strip()
    port_number = None
    if path.startswith("/dev/ttyMXUSB"):
        try:
            port_number = int(path.rsplit("ttyMXUSB", 1)[1]) + 1
        except (IndexError, ValueError):
            port_number = None
    marker = "Port: "
    raw_value = None
    if marker in line:
        raw = line.split(marker, 1)[1].split(",", 1)[0].strip()
        try:
            raw_value = int(raw, 16)
        except ValueError:
            raw_value = None
    if port_number is None:
        return None, None
    label = SERIAL_INTERFACE_LABELS.get(raw_value, "Okänd")
    return port_number, {
        "raw": f"0x{raw_value:x}" if raw_value is not None else None,
        "label": label,
        "text": f"{label} (0x{raw_value:x})" if raw_value is not None else "Okänd",
        "ok": raw_value == 0x1,
    }


def load_serial_configs():
    """Read Moxa interface mode for every port via setserial.

    Moxa's Linux driver overloads the `setserial port` value to mean interface
    type: 0x0 RS232, 0x1 RS485 2-wire, 0x2 RS422, 0x3 RS485 4-wire.
    """
    now = time.time()
    if _serial_config_cache["expires_at"] > now:
        return _serial_config_cache["ports"]
    paths = [moxa_tty(port_number) for port_number in range(1, MOXA_PORT_COUNT + 1)]
    configs = {}
    try:
        output = subprocess.check_output(["setserial", "-g", *paths], text=True, stderr=subprocess.STDOUT, timeout=2.0)
        for line in output.splitlines():
            port_number, config = parse_setserial_line(line)
            if port_number is not None and config:
                configs[port_number] = config
    except Exception as exc:
        for port_number in range(1, MOXA_PORT_COUNT + 1):
            configs[port_number] = {
                "raw": None,
                "label": "Okänd",
                "text": f"Kunde inte läsa setserial: {exc}",
                "ok": False,
            }
    _serial_config_cache["expires_at"] = now + SERIAL_CONFIG_CACHE_S
    _serial_config_cache["ports"] = configs
    return configs


def normalized_moxa_port(value):
    """Validate a stored Moxa port number and return None if invalid."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= MOXA_PORT_COUNT else None


def load_port_activity():
    """Load best-effort Modbus activity emitted by the serial simulator."""
    try:
        with open(PORT_ACTIVITY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    activity = {}
    now = time.time()
    for item in data.get("ports", []) if isinstance(data, dict) else []:
        port = normalized_moxa_port(item.get("moxa_port"))
        if port is None:
            continue
        last_request_at = item.get("last_request_at")
        active = isinstance(last_request_at, (int, float)) and now - float(last_request_at) <= PORT_ACTIVE_WINDOW_S
        enriched = dict(item)
        enriched["rs485_active"] = active
        enriched["last_request_at_ms"] = int(float(last_request_at) * 1000) if isinstance(last_request_at, (int, float)) else None
        last_identity_at = item.get("last_identity_at")
        enriched["last_identity_at_ms"] = int(float(last_identity_at) * 1000) if isinstance(last_identity_at, (int, float)) else None
        activity[port] = enriched
    return activity


def device_key(device):
    """Use the stable Wall Connector serial number when available."""
    return device.get("serial") or device.get("ip")


def with_moxa_metadata(device, port_number):
    """Attach stable display metadata for one physical Moxa port."""
    enriched = dict(device)
    enriched["moxa_port"] = port_number
    enriched["moxa_model"] = MOXA_MODEL
    enriched["moxa_label"] = moxa_label(port_number)
    enriched["tty"] = moxa_tty(port_number)
    return enriched


def assign_moxa_ports(devices):
    """Attach stable Moxa port metadata to known Wall Connectors.

    The Wall Connector HTTP API reports charger status over Wi-Fi, but it does
    not know which isolated RS485 port on the Moxa adapter the charger uses.
    That association is local installation state, persisted as moxa_port in
    known_wall_connectors.json. Existing mappings win; devices without one get
    the first free port in list order during bring-up.
    """
    used = set()
    assigned = []
    for device in devices:
        port = normalized_moxa_port(device.get("moxa_port"))
        if port is not None and port not in used:
            used.add(port)
            assigned.append(with_moxa_metadata(device, port))
        else:
            assigned.append(dict(device))

    next_port = 1
    completed = []
    for device in assigned:
        port = normalized_moxa_port(device.get("moxa_port"))
        if port is None:
            while next_port in used and next_port <= MOXA_PORT_COUNT:
                next_port += 1
            if next_port <= MOXA_PORT_COUNT:
                port = next_port
                used.add(port)
                next_port += 1
        completed.append(with_moxa_metadata(device, port) if port is not None else device)
    return completed


def build_moxa_slots(devices):
    """Return the fixed 16-port Moxa layout consumed by the frontend."""
    activity = load_port_activity()
    serial_configs = load_serial_configs()
    slots = [
        {
            "moxa_port": port_number,
            "moxa_model": MOXA_MODEL,
            "moxa_label": moxa_label(port_number),
            "tty": moxa_tty(port_number),
            "serial_config": serial_configs.get(port_number, {
                "raw": None,
                "label": "Okänd",
                "text": "Okänd",
                "ok": False,
            }),
            "identity_serial": activity.get(port_number, {}).get("identity_serial"),
            "activity": activity.get(port_number, {}),
            "rs485_active": bool(activity.get(port_number, {}).get("rs485_active")),
            "occupied": False,
        }
        for port_number in range(1, MOXA_PORT_COUNT + 1)
    ]
    for device in assign_moxa_ports(devices):
        port = normalized_moxa_port(device.get("moxa_port"))
        if port is None:
            continue
        slots[port - 1]["occupied"] = True
        slots[port - 1]["device"] = device
    return slots


def save_device_name(payload):
    """Persist a human-friendly name on the Wall Connector identity, not port."""
    key = str(payload.get("key", "")).strip()
    display_name = str(payload.get("display_name", "")).strip()[:80]
    if not key:
        raise ValueError("device key is required")
    devices = load_known_devices()
    updated = False
    for device in devices:
        if device_key(device) == key:
            if display_name:
                device["display_name"] = display_name
            else:
                device.pop("display_name", None)
            updated = True
    if not updated:
        raise ValueError("device was not found")
    save_known_devices(devices)
    refreshed = refresh_known_devices()
    return refreshed


def load_known_devices():
    """Load persisted Wall Connector IP/serial hints."""
    try:
        with open(KNOWN_DEVICES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return assign_moxa_ports(data) if isinstance(data, list) else []
    except Exception:
        return []


def save_known_devices(devices):
    """Persist discovered Wall Connectors without volatile online/status data."""
    try:
        by_serial_or_ip = {}
        for dev in assign_moxa_ports(load_known_devices() + devices):
            key = device_key(dev)
            if not key:
                continue
            keep = dict(dev)
            keep.pop("online", None)
            by_serial_or_ip[key] = keep
        with open(KNOWN_DEVICES_PATH, "w", encoding="utf-8") as f:
            json.dump(assign_moxa_ports(list(by_serial_or_ip.values())), f, indent=2)
            f.write("\n")
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
    known_by_key = {device_key(dev): dev for dev in load_known_devices() if device_key(dev)}
    for dev in assign_moxa_ports(found):
        key = device_key(dev)
        if key in known_by_key:
            known = dict(known_by_key[key])
            known.update(dev)
            dev = known
        seen.add(key)
        merged.append(dev)
    for dev in load_known_devices():
        key = device_key(dev)
        if key in seen:
            continue
        offline = dict(dev)
        offline["online"] = False
        merged.append(offline)
    return assign_moxa_ports(merged)


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
    refreshed = assign_moxa_ports(refreshed)
    refreshed.sort(key=lambda d: normalized_moxa_port(d.get("moxa_port")) or 999)
    return {
        "duration_s": round(time.time() - started, 2),
        "devices": refreshed,
        "ports": build_moxa_slots(refreshed),
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
    devices = assign_moxa_ports(devices)
    devices.sort(key=lambda d: normalized_moxa_port(d.get("moxa_port")) or 999)
    return {
        "subnet": str(network),
        "hosts_scanned": len(hosts),
        "duration_s": round(time.time() - started, 2),
        "devices": devices,
        "ports": build_moxa_slots(devices),
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
        if parsed.path == "/api/fronius":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8"))
                save_fronius_config(payload)
                self.send_json(200, get_fronius_status(update_neurio=True))
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/control":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8"))
                save_control_config(payload)
                self.send_json(200, control_status())
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/device-name":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8"))
                self.send_json(200, save_device_name(payload))
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/identify/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8"))
                self.send_json(200, start_identify_signatures(payload))
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
        if parsed.path == "/api/fronius":
            self.send_json(200, get_fronius_status(update_neurio=True))
            return
        if parsed.path == "/api/control":
            self.send_json(200, control_status())
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
