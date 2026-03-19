// ======================
// CONFIG
// ======================
const BACKEND_BASE = "http://127.0.0.1:5000";
const ROADS_ENDPOINT = "/roads";        // draw roads
const HEAT_ENDPOINT = "/road-heat";     // road heat data
const SIM_ENDPOINT  = "/simulate";      // individual car positions
const SIGNALS_ENDPOINT = "/signals";    // 🚦 traffic lights
const STOPS_ENDPOINT   = "/stops";      // 🛑 stop signs (priority intersections)
const MULT_ENDPOINT    = "/multipliers"; // ⏱️ hour -> volume/speed multipliers
const WAIT_STATS_ENDPOINT = "/wait-stats"; // 📊 signal wait data
const SET_MODE_ENDPOINT   = "/set-signal-mode"; // 🤖 switch signal model
const NODES_ENDPOINT      = "/nodes";      // 🔵 all intersection nodes

// How often we update
const HEAT_MS = 800;    // heatmap refresh
const FETCH_MS = 400;   // car fetch refresh
const SIGNALS_MS = 500; // 🚦 light refresh

// Smoothing time constant (ms) for car markers
const CHASE_TAU_MS = 900;

// ======================
// STATE
// ======================
let map;
let roadsLayer;     // base roads (always neutral)
let heatLayer;      // overlay heat layer
let carsLayer;
let signalsLayer;   // 🚦 signal markers layer
let stopsLayer;     // 🛑 stop sign markers layer
let canvasRenderer;

let currentHour = 12;

// viewMode: "heat" or "cars"
let viewMode = "heat";

// heat mode state
let heatRunning = false;
let heatTimer = null;

// cars mode state
let carsRunning = false;
let simTimer = null;
let lastFrameMs = null;

// 🚦 signals polling
let signalsRunning = false;
let signalsTimer = null;

// 📊 wait stats polling
let waitStatsTimer = null;
const WAIT_STATS_MS = 1500;

// 🤖 signal model mode
let currentSignalMode = "pretrained";

let centeredOnce = false;

// Base road polylines (neutral)
const roadLines = new Map(); // edgeId -> polyline

// Heat overlay polylines (colored by congestion)
const heatLines = new Map(); // edgeId -> polyline

// Store car markers + smoothing state
const cars = new Map();

// 🚦 Store signal markers
const signalMarkers = new Map();

// 🛑 Store stop sign markers
const stopMarkers = new Map();

// 🔵 All-nodes hover layer
let nodesLayer;

// ======================
// UTIL
// ======================
function setStatus(msg) {
  const el = document.getElementById("status");
  if (el) el.textContent = msg;
}

async function fetchJSON(path) {
  const res = await fetch(BACKEND_BASE + path);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`GET ${path} -> ${res.status} ${text}`);
  }
  return res.json();
}

// Backend sends edges with e.coords = [{lat,lon}, ...]
function parseEdges(roadsJson) {
  const edges = roadsJson.edges ?? [];
  return edges
    .map(e => {
      const coords = (e.coords ?? [])
        .map(p => [Number(p.lat), Number(p.lon)])
        .filter(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon));
      return { id: e.id ?? "", coords };
    })
    .filter(e => e.id && e.coords.length >= 2);
}

// cars from /simulate
function parseCars(simJson) {
  return (simJson ?? [])
    .map(c => ({ id: c.id, lat: c.lat, lon: c.lon, teleport: !!c.teleport }))
    .filter(c => typeof c.lat === "number" && typeof c.lon === "number");
}

function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

function lerp(a, b, t) { return a + (b - a) * t; }

function alphaFromDt(dtMs, tauMs) {
  return 1 - Math.exp(-dtMs / Math.max(1, tauMs));
}

// Green (low) -> Yellow (medium) -> Red (high)
function heatColor(t) {
  t = clamp01(t);

  if (t <= 0.5) {
    const a = t / 0.5;
    const r = Math.round(255 * a);
    const g = 255;
    return `rgb(${r},${g},0)`;
  } else {
    const a = (t - 0.5) / 0.5;
    const r = 255;
    const g = Math.round(255 * (1 - a));
    return `rgb(${r},${g},0)`;
  }
}

// ======================
// ⏱️ TIME UI + MULTIPLIERS UI
// ======================
function formatHourLabel(hour24) {
  const h = Number(hour24) % 24;
  const displayHour = h % 12 || 12;
  const ampm = h >= 12 ? "PM" : "AM";
  return `${displayHour}:00 ${ampm}`;
}

async function refreshMultipliersUI() {
  const volEl = document.getElementById("vol-mult");
  const spdEl = document.getElementById("speed-mult");
  if (!volEl || !spdEl) return;

  try {
    const m = await fetchJSON(`${MULT_ENDPOINT}?hour=${currentHour}`);
    const v = Number(m.volume_multiplier ?? 1);
    const s = Number(m.speed_multiplier ?? 1);

    // Display as %
    volEl.textContent = `${Math.round(v * 100)}%`;
    spdEl.textContent = `${Math.round(s * 100)}%`;
  } catch (e) {
    console.warn("Multipliers UI error:", e);
  }
}

// ======================
// MAP INIT
// ======================
function initMap() {
  map = L.map("map", { preferCanvas: true, zoomControl: false }).setView([37.951, -91.771], 14);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap"
  }).addTo(map);

  canvasRenderer = L.canvas({ padding: 0.5 });

  // Base roads always on
  roadsLayer = L.layerGroup().addTo(map);

  // Heat overlay NOT added by default (so cars mode never shows heat)
  heatLayer = L.layerGroup();

  // Cars layer always exists, but only used in cars mode
  carsLayer = L.layerGroup().addTo(map);

  // 🚦 Signals layer (can be toggled)
  signalsLayer = L.layerGroup().addTo(map);

  // 🛑 Stop signs layer (can be toggled)
  stopsLayer = L.layerGroup().addTo(map);

  // 🔵 Free-flow nodes hover layer (added last so it's on top, but only contains free-flow nodes)
  nodesLayer = L.layerGroup().addTo(map);
}

// ======================
// ROADS (draw once, store polylines)
// ======================
async function loadRoads() {
  setStatus("Loading roads...");
  const roadsJson = await fetchJSON(ROADS_ENDPOINT);
  const edges = parseEdges(roadsJson);

  roadsLayer.clearLayers();
  heatLayer.clearLayers();
  roadLines.clear();
  heatLines.clear();

  if (edges.length === 0) {
    console.log("ROADS RAW:", roadsJson);
    setStatus("No drawable roads (check console).");
    return;
  }

  const allPoints = [];

  edges.forEach((edge, idx) => {
    allPoints.push(...edge.coords);

    const baseLine = L.polyline(edge.coords, {
      color: "#555",
      weight: 3,
      opacity: 0.55,
      renderer: canvasRenderer
    }).addTo(roadsLayer);

    const heatLine = L.polyline(edge.coords, {
      color: "rgb(0,255,0)",
      weight: 5,
      opacity: 0.0,
      renderer: canvasRenderer
    });

    heatLine.bindTooltip(edge.id ? `Road ${edge.id}` : `Road ${idx}`, { sticky: true });

    roadLines.set(edge.id, baseLine);
    heatLines.set(edge.id, heatLine);
  });

  if (!centeredOnce && allPoints.length > 0) {
    map.fitBounds(L.latLngBounds(allPoints).pad(0.12));
    centeredOnce = true;
  }

  setStatus(`Roads loaded: ${edges.length} edges`);
}

// ======================
// HEAT LAYER SHOW/HIDE
// ======================
function showHeatOverlay() {
  if (heatLayer.getLayers().length === 0) {
    for (const line of heatLines.values()) heatLayer.addLayer(line);
  }
  if (!map.hasLayer(heatLayer)) heatLayer.addTo(map);
}

function hideHeatOverlay() {
  if (map.hasLayer(heatLayer)) map.removeLayer(heatLayer);
}

// ======================
// 🚦 SIGNALS (traffic lights UI)
// ======================
function makeSignalIcon(nsColor, ewColor) {
  const nsGreen = nsColor === "rgb(0,255,0)";
  const ewGreen = ewColor === "rgb(0,255,0)";

  function row(color, isGreen, label) {
    const dimmed = isGreen ? "1" : "0.3";
    return `
      <div style="display:flex;align-items:center;gap:3px;opacity:${dimmed}">
        <div style="
          width:8px;height:8px;flex-shrink:0;border-radius:50%;
          background:${color};
          box-shadow:${isGreen ? `0 0 5px ${color}` : "none"};
        "></div>
        <span style="
          font:bold 8px/1 monospace;
          color:${color};
          white-space:nowrap;
        ">${label}</span>
      </div>`;
  }

  const html = `
    <div style="
      padding:3px 5px;
      background:rgba(0,0,0,0.72);
      border:1px solid rgba(255,255,255,0.18);
      border-radius:6px;
      display:flex;flex-direction:column;gap:3px;
      box-shadow:0 0 8px rgba(0,0,0,0.5);
      pointer-events:none;
    ">
      ${row(nsColor, nsGreen, "N↑ S↓")}
      ${row(ewColor, ewGreen, "E→ W←")}
    </div>`;

  return L.divIcon({
    className: "",
    html,
    iconSize: [62, 36],
    iconAnchor: [31, 18]
  });
}

async function pollSignalsOnce() {
  const list = await fetchJSON(SIGNALS_ENDPOINT);
  const seen = new Set();

  for (const s of (list ?? [])) {
    const id = String(s.id);
    const lat = Number(s.lat);
    const lon = Number(s.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

    seen.add(id);

    const ns = (s.ns === "green") ? "rgb(0,255,0)" : "rgb(255,0,0)";
    const ew = (s.ew === "green") ? "rgb(0,255,0)" : "rgb(255,0,0)";
    const icon = makeSignalIcon(ns, ew);

    if (!signalMarkers.has(id)) {
      const m = L.marker([lat, lon], { icon }).addTo(signalsLayer);

      signalMarkers.set(id, m);
    } else {
      const m = signalMarkers.get(id);
      m.setLatLng([lat, lon]);
      m.setIcon(icon);

    }
  }

  for (const [id, m] of signalMarkers.entries()) {
    if (!seen.has(id)) {
      m.closeTooltip();
      signalsLayer.removeLayer(m);
      signalMarkers.delete(id);
    }
  }
}

function startSignals() {
  if (signalsRunning) return;
  signalsRunning = true;

  pollSignalsOnce().catch(err => console.warn("Signals error:", err));
  signalsTimer = setInterval(() => {
    pollSignalsOnce().catch(err => console.warn("Signals error:", err));
  }, SIGNALS_MS);
}

function stopSignals() {
  signalsRunning = false;
  if (signalsTimer) clearInterval(signalsTimer);
  signalsTimer = null;
}

// ======================
// 🛑 STOP SIGNS (priority intersections)
// ======================
function makeStopSignIcon() {
  const html = `
    <div style="
      width:22px; height:22px;
      display:flex; align-items:center; justify-content:center;
      background:#d40000;
      color:#fff;
      font-weight:800;
      font-size:9px;
      border:2px solid rgba(255,255,255,0.85);
      box-shadow: 0 0 6px rgba(212,0,0,0.55);
      clip-path: polygon(
        30% 0%, 70% 0%,
        100% 30%, 100% 70%,
        70% 100%, 30% 100%,
        0% 70%, 0% 30%
      );
      letter-spacing:0.5px;
      user-select:none;
      pointer-events:none;
    ">STOP</div>
  `;

  return L.divIcon({
    className: "",
    html,
    iconSize: [22, 22],
    iconAnchor: [11, 11]
  });
}

async function loadStopsOnce() {
  const list = await fetchJSON(STOPS_ENDPOINT);
  const icon = makeStopSignIcon();

  stopsLayer.clearLayers();
  stopMarkers.clear();

  for (const s of (list ?? [])) {
    const id = String(s.id);
    const lat = Number(s.lat);
    const lon = Number(s.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

    const m = L.marker([lat, lon], { icon }).addTo(stopsLayer);
    stopMarkers.set(id, m);
  }
}

// ======================
// 🔵 ALL-NODES CLICK POPUP LAYER
// ======================

// Custom click popup — one shared div, shown near the click position
const nodePopup = document.createElement("div");
nodePopup.id = "node-popup";
nodePopup.style.cssText = [
  "position:fixed",
  "z-index:9999",
  "pointer-events:none",
  "display:none",
  "background:rgba(15,15,25,0.93)",
  "color:#e8e8f0",
  "border:1px solid rgba(255,255,255,0.13)",
  "border-radius:9px",
  "padding:8px 13px",
  "font:13px/1.5 system-ui,sans-serif",
  "box-shadow:0 4px 18px rgba(0,0,0,0.45)",
  "white-space:nowrap",
  "max-width:260px",
].join(";");
document.body.appendChild(nodePopup);

function showNodePopup(html, mouseEvent) {
  nodePopup.innerHTML = html;
  nodePopup.style.display = "block";
  positionNodePopup(mouseEvent);
}

function positionNodePopup(e) {
  const pad = 14;
  const pw  = nodePopup.offsetWidth  || 200;
  const ph  = nodePopup.offsetHeight || 60;
  let x = e.clientX + pad;
  let y = e.clientY - ph / 2;
  if (x + pw > window.innerWidth  - pad) x = e.clientX - pw - pad;
  if (y < pad)                           y = pad;
  if (y + ph > window.innerHeight - pad) y = window.innerHeight - ph - pad;
  nodePopup.style.left = x + "px";
  nodePopup.style.top  = y + "px";
}

function hideNodePopup() {
  nodePopup.style.display = "none";
}

// Dismiss popup on map click (not on a node)
document.addEventListener("click", (e) => {
  if (!e._fromNode) hideNodePopup();
});

// Returns a pixel radius that grows as you zoom in, making nodes easier to click
function nodeHitRadius() {
  const zoom = map.getZoom();
  // At zoom 14 (default) → 18px. Each zoom level doubles the scale,
  // so we scale radius proportionally: radius = base * 2^(zoom - baseZoom)
  const BASE_ZOOM = 14;
  const BASE_RADIUS = 28;
  const scaled = BASE_RADIUS * Math.pow(2, zoom - BASE_ZOOM);
  // Clamp between 12px (zoomed way out) and 80px (zoomed way in)
  return Math.max(12, Math.min(80, scaled));
}

// Store node data so we can update radii on zoom
const nodeMarkers = []; // { marker, ... }

async function loadNodesOnce() {
  const list = await fetchJSON(NODES_ENDPOINT);
  nodesLayer.clearLayers();
  nodeMarkers.length = 0;

  for (const n of (list ?? [])) {
    const id      = String(n.id);
    const lat     = Number(n.lat);
    const lon     = Number(n.lon);
    const control = n.control ?? "none";
    const degree  = n.degree  ?? 0;
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

    const typeLabel = control === "signal"
      ? "🚦 Signalized Intersection"
      : control === "priority"
        ? "🛑 Stop / Yield Intersection"
        : "⬤ Free-Flow Intersection";

    const m = L.circleMarker([lat, lon], {
      radius: nodeHitRadius(),
      color: "transparent",
      fillColor: "transparent",
      fillOpacity: 0,
      weight: 0,
      renderer: canvasRenderer,
      interactive: true,
    }).addTo(nodesLayer);

    m.on("click", (e) => {
      e.originalEvent._fromNode = true;
      const html = `<b>${typeLabel}</b><br>`
        + `<span style="opacity:0.6;font-size:11px">Roads: ${degree} &nbsp;|&nbsp; ID ${id}</span>`;
      showNodePopup(html, e.originalEvent);
    });

    nodeMarkers.push(m);
  }

  // Update all node hitbox radii whenever the map zoom changes
  map.off("zoomend", updateNodeRadii); // avoid duplicate listeners
  map.on("zoomend", updateNodeRadii);
}

function updateNodeRadii() {
  const r = nodeHitRadius();
  for (const m of nodeMarkers) {
    m.setRadius(r);
  }
}

// ======================
// HEATMAP (style heat overlay lines)
// ======================
async function pollHeatOnce() {
  const heatJson = await fetchJSON(`${HEAT_ENDPOINT}?hour=${currentHour}`);

  const heatMap = heatJson.heat ?? {};
  const countMap = heatJson.counts ?? {};

  const FULL_RED_AT = 1.5;

  for (const [edgeId, line] of heatLines.entries()) {
    const ratio = Number(heatMap[edgeId] ?? 0);
    const c = Number(countMap[edgeId] ?? 0);

    const t = clamp01(ratio / FULL_RED_AT);

    line.setStyle({
      color: heatColor(t),
      weight: 3 + 7 * t,
      opacity: 0.25 + 0.75 * t
    });

    line.bindTooltip(
      `${edgeId} • cars: ${c} • congestion: ${ratio.toFixed(2)}`,
      { sticky: true }
    );
  }

  setStatus("Heatmap updated");
}

// ======================
// CARS VIEW (individual cars)
// ======================
function clearCars() {
  for (const car of cars.values()) carsLayer.removeLayer(car.marker);
  cars.clear();
}

async function pollSimOnce() {
  const simJson = await fetchJSON(`${SIM_ENDPOINT}?hour=${currentHour}`);
  const list = parseCars(simJson);

  const seen = new Set();

  for (const p of list) {
    seen.add(p.id);

    if (!cars.has(p.id)) {
      const marker = L.circleMarker([p.lat, p.lon], {
        radius: 5,
        renderer: canvasRenderer
      }).addTo(carsLayer);

      cars.set(p.id, {
        marker,
        target: { lat: p.lat, lon: p.lon },
        smooth: { lat: p.lat, lon: p.lon }
      });
    } else {
      const car = cars.get(p.id);
      car.target.lat = p.lat;
      car.target.lon = p.lon;

      if (p.teleport) {
        car.smooth.lat = p.lat;
        car.smooth.lon = p.lon;
        car.marker.setLatLng([p.lat, p.lon]);
      }
    }
  }

  for (const [id, car] of cars.entries()) {
    if (!seen.has(id)) {
      carsLayer.removeLayer(car.marker);
      cars.delete(id);
    }
  }

  setStatus(`Cars updated (${list.length})`);
}

function animateCarsFrame(nowMs) {
  if (!carsRunning) return;

  if (lastFrameMs == null) lastFrameMs = nowMs;
  const dt = Math.min(100, Math.max(0, nowMs - lastFrameMs));
  lastFrameMs = nowMs;

  const a = alphaFromDt(dt, CHASE_TAU_MS);

  for (const car of cars.values()) {
    car.smooth.lat = lerp(car.smooth.lat, car.target.lat, a);
    car.smooth.lon = lerp(car.smooth.lon, car.target.lon, a);
    car.marker.setLatLng([car.smooth.lat, car.smooth.lon]);
  }

  requestAnimationFrame(animateCarsFrame);
}

// ======================
// START / STOP (by mode)
// ======================
function startHeatmap() {
  if (heatRunning) return;
  heatRunning = true;

  showHeatOverlay();

  document.getElementById("btn-toggle-sim").textContent = "Stop";
  setStatus("Heatmap running...");

  startSignals();

  pollHeatOnce().catch(err => setStatus(`Heat error: ${err.message}`));
  heatTimer = setInterval(() => {
    pollHeatOnce().catch(err => setStatus(`Heat error: ${err.message}`));
  }, HEAT_MS);
  startWaitStats();
}

function stopHeatmap() {
  heatRunning = false;
  if (heatTimer) clearInterval(heatTimer);
  heatTimer = null;

  stopSignals();
  stopWaitStats();
}

function startCars() {
  if (carsRunning) return;
  carsRunning = true;

  hideHeatOverlay();

  document.getElementById("btn-toggle-sim").textContent = "Stop";
  setStatus("Cars view running...");

  startSignals();

  lastFrameMs = null;
  requestAnimationFrame(animateCarsFrame);

  pollSimOnce().catch(err => setStatus(`Sim error: ${err.message}`));
  simTimer = setInterval(() => {
    pollSimOnce().catch(err => setStatus(`Sim error: ${err.message}`));
  }, FETCH_MS);
  startWaitStats();
}

function stopCars() {
  carsRunning = false;
  if (simTimer) clearInterval(simTimer);
  simTimer = null;
  clearCars();

  stopSignals();
  stopWaitStats();
}

function startCurrentMode() {
  if (viewMode === "cars") startCars();
  else startHeatmap();
}

function stopCurrentMode() {
  if (viewMode === "cars") stopCars();
  else stopHeatmap();
}

function setMode(newMode) {
  if (newMode === viewMode) return;

  stopCurrentMode();

  if (newMode === "cars") {
    hideHeatOverlay();
    clearCars();
  } else {
    clearCars();
    showHeatOverlay();
    hideHeatOverlay();
  }

  viewMode = newMode;

  const btn = document.getElementById("btn-toggle-sim");
  if (btn) btn.textContent = "Start";

  setStatus(`Mode: ${viewMode === "cars" ? "Cars" : "Heatmap"} (press Start)`);
}

// ======================
// UI
// ======================
function initUI() {
  // ⏱️ time slider
  const hourSlider = document.getElementById("hourSlider");
  const hourLabel  = document.getElementById("hourLabel");

  if (hourSlider) {
    currentHour = Number(hourSlider.value ?? 12);
    if (hourLabel) hourLabel.textContent = formatHourLabel(currentHour);

    // initialize stats
    refreshMultipliersUI();

    hourSlider.oninput = function () {
      currentHour = Number(this.value ?? 12);
      if (hourLabel) hourLabel.textContent = formatHourLabel(currentHour);

      // update multipliers display
      refreshMultipliersUI();

      setStatus(`Time set to ${formatHourLabel(currentHour)}`);
    };
  } else {
    // If the slider isn't in the HTML, just set default multipliers display if elements exist
    refreshMultipliersUI();
  }

  document.getElementById("btn-refresh").onclick = () =>
    loadRoads().catch(err => setStatus(`Road error: ${err.message}`));

  document.getElementById("btn-toggle-sim").onclick = () => {
    const running = (viewMode === "cars") ? carsRunning : heatRunning;
    if (running) {
      stopCurrentMode();
      document.getElementById("btn-toggle-sim").textContent = "Start";
      setStatus("Stopped.");
    } else {
      startCurrentMode();
    }
  };

  const viewToggle = document.getElementById("viewToggle");
  const viewLabel = document.getElementById("viewLabel");

  if (viewToggle) {
    viewToggle.checked = false;
    if (viewLabel) viewLabel.textContent = "Heatmap";

    hideHeatOverlay();
    setMode("heat");

    viewToggle.onchange = () => {
      if (viewToggle.checked) {
        if (viewLabel) viewLabel.textContent = "Cars";
        setMode("cars");
      } else {
        if (viewLabel) viewLabel.textContent = "Heatmap";
        setMode("heat");
      }
    };
  }

  // 🚦 show/hide traffic lights without stopping sim
  const lightsToggle = document.getElementById("lightsToggle");
  if (lightsToggle) {
    lightsToggle.checked = true;
    lightsToggle.addEventListener("change", () => {
      if (lightsToggle.checked) {
        map.addLayer(signalsLayer);
      } else {
        map.removeLayer(signalsLayer);
      }
    });
  }

  // 🛑 show/hide stop signs without stopping sim
  const stopsToggle = document.getElementById("stopsToggle");
  if (stopsToggle) {
    stopsToggle.checked = true;
    stopsToggle.addEventListener("change", () => {
      if (stopsToggle.checked) map.addLayer(stopsLayer);
      else map.removeLayer(stopsLayer);
    });
  }

  // 🤖 signal mode toggle button
  const signalModeBtn = document.getElementById("btn-signal-mode");
  if (signalModeBtn) {
    updateSignalModeBtn(currentSignalMode);
    signalModeBtn.addEventListener("click", toggleSignalMode);
  }

  // 📊 wait stats panel collapse/expand
  const collapseBtn = document.getElementById("btn-wait-collapse");
  const waitPanel   = document.getElementById("wait-stats-panel");
  if (collapseBtn && waitPanel) {
    collapseBtn.addEventListener("click", () => {
      const isCollapsed = waitPanel.classList.toggle("collapsed");
      collapseBtn.title = isCollapsed ? "Show panel" : "Hide panel";
    });
  }

  // ⏱️ time controls collapse/expand
  const timeCollapseBtn = document.getElementById("btn-time-collapse");
  const timePanel       = document.getElementById("time-controls");
  if (timeCollapseBtn && timePanel) {
    timeCollapseBtn.addEventListener("click", () => {
      const isCollapsed = timePanel.classList.toggle("collapsed");
      timeCollapseBtn.title = isCollapsed ? "Show panel" : "Hide panel";
    });
  }
}

// ======================
// BOOT
// ======================
window.addEventListener("load", async () => {
  initMap();
  initUI();

  await loadRoads().catch(err => setStatus(`Road load failed: ${err.message}`));

  // 🛑 load stop signs once (static)
  await loadStopsOnce().catch(err => console.warn("Stops error:", err));

  // 🔵 load all intersection nodes for hover tooltips
  await loadNodesOnce().catch(err => console.warn("Nodes error:", err));

  hideHeatOverlay();
  setStatus("Mode: Heatmap (press Start)");
});
// ======================
// 📊 WAIT STATS
// ======================

function waitColor(avgSecs) {
  if (avgSecs < 10) return "#4ade80";  // green
  if (avgSecs < 18) return "#facc15";  // yellow
  return "#f87171";                    // red
}

function updateWaitStatsUI(data) {
  const avgEl  = document.getElementById("city-avg-wait");
  const maxEl  = document.getElementById("city-max-wait");
  const vehEl  = document.getElementById("city-total-vehs");
  const timeEl = document.getElementById("wait-sim-time");
  const badge  = document.getElementById("wait-mode-badge");
  const tbody  = document.getElementById("wait-table-body");

  if (!avgEl) return;

  // City-wide numbers
  const avg = data.city_avg_wait ?? 0;
  const max = data.city_max_wait ?? 0;
  avgEl.textContent  = avg > 0 ? `${avg.toFixed(1)}s` : "—";
  avgEl.style.color  = avg > 0 ? waitColor(avg) : "#fff";
  maxEl.textContent  = max > 0 ? `${max.toFixed(1)}s` : "—";
  maxEl.style.color  = max > 0 ? waitColor(max) : "#fff";
  vehEl.textContent  = (data.city_total_vehicles ?? 0).toLocaleString();
  if (timeEl) timeEl.textContent = `${(data.sim_time ?? 0).toFixed(0)}s`;

  // Mode badge
  if (badge) {
    const mode = (data.signal_mode ?? "fixed").toLowerCase();
    badge.textContent = mode.toUpperCase();
    badge.className = `mode-badge ${mode}`;
  }

  // Per-intersection table
  if (!tbody) return;

  const nodes = data.nodes ?? {};
  const rows = Object.entries(nodes)
    .map(([id, s]) => ({ id, ...s }))
    .sort((a, b) => b.avg - a.avg)
    .slice(0, 20);

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;opacity:0.4;padding:10px">No wait data yet</td></tr>`;
    return;
  }

  const maxAvg = rows[0]?.avg ?? 1;
  tbody.innerHTML = rows.map(r => {
    const barW = Math.round((r.avg / Math.max(maxAvg, 1)) * 60);
    const col  = waitColor(r.avg);
    const nsPct = Math.round((r.ns_split ?? 0.5) * 100);
    const ewPct = 100 - nsPct;
    return `
      <tr>
        <td style="opacity:0.6;font-size:11px">${r.id.slice(-8)}</td>
        <td>
          <span class="wait-bar" style="width:${barW}px;background:${col}"></span>
          <span style="color:${col};font-weight:700">${r.avg.toFixed(1)}s</span>
        </td>
        <td style="opacity:0.7">${r.max.toFixed(1)}s</td>
        <td style="opacity:0.7">${r.cycle}s</td>
        <td style="font-size:11px;opacity:0.7">${nsPct}N/${ewPct}E</td>
      </tr>`;
  }).join("");
}

function resetWaitStatsUI(newMode) {
  const avgEl  = document.getElementById("city-avg-wait");
  const maxEl  = document.getElementById("city-max-wait");
  const vehEl  = document.getElementById("city-total-vehs");
  const timeEl = document.getElementById("wait-sim-time");
  const badge  = document.getElementById("wait-mode-badge");
  const tbody  = document.getElementById("wait-table-body");

  if (avgEl) { avgEl.textContent = "—"; avgEl.style.color = "#fff"; }
  if (maxEl) { maxEl.textContent = "—"; maxEl.style.color = "#fff"; }
  if (vehEl)  vehEl.textContent  = "—";
  if (timeEl) timeEl.textContent = "0s";

  if (badge && newMode) {
    badge.textContent = newMode.toUpperCase();
    badge.className   = `mode-badge ${newMode.toLowerCase()}`;
  }

  if (tbody) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;opacity:0.4;padding:10px">Switched model — accumulating new data…</td></tr>`;
  }
}

async function pollWaitStats() {
  try {
    const data = await fetchJSON(WAIT_STATS_ENDPOINT);
    updateWaitStatsUI(data);
    // keep currentSignalMode in sync
    currentSignalMode = data.signal_mode ?? currentSignalMode;
  } catch (e) {
    console.warn("Wait stats error:", e);
  }
}

function startWaitStats() {
  if (waitStatsTimer) return;
  pollWaitStats();
  waitStatsTimer = setInterval(pollWaitStats, WAIT_STATS_MS);
}

function stopWaitStats() {
  if (waitStatsTimer) clearInterval(waitStatsTimer);
  waitStatsTimer = null;
}

// ======================
// 🤖 SIGNAL MODE TOGGLE
// ======================

function updateSignalModeBtn(mode) {
  const btn   = document.getElementById("btn-signal-mode");
  const icon  = document.getElementById("signal-mode-icon");
  const label = document.getElementById("signal-mode-label");
  if (!btn) return;

  if (mode === "fixed") {
    icon.textContent  = "🔧";
    label.textContent = "Fixed";
    btn.classList.add("fixed-mode");
  } else {
    icon.textContent  = "📂";
    label.textContent = "Pretrained";
    btn.classList.remove("fixed-mode");
  }
}

async function toggleSignalMode() {
  const nextMode = (currentSignalMode === "fixed") ? "pretrained" : "fixed";
  try {
    setStatus(`Switching to ${nextMode} signal model...`);
    const res = await fetch(BACKEND_BASE + SET_MODE_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: nextMode })
    });
    const data = await res.json();
    currentSignalMode = data.signal_mode ?? nextMode;
    updateSignalModeBtn(currentSignalMode);
    // Clear stale stats from the previous model immediately
    resetWaitStatsUI(currentSignalMode);
    setStatus(`Signal model: ${currentSignalMode.toUpperCase()} — wait data reset`);
    // Resume polling so new data starts filling in right away
    pollWaitStats();
  } catch (e) {
    setStatus(`Mode switch failed: ${e.message}`);
  }
}
