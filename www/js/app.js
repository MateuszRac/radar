// ── Orkiestracja aplikacji ────────────────────────────────────────────────────
import {
  PALETTES, FORECAST_TYPES, productByKey, colorToEntry,
  SPEED_STEPS, DEFAULT_SPEED_IDX, DEFAULT_OPACITY, REFRESH_MS,
  LS_OPACITY, LS_SPEED, LS_STYLE,
} from './config.js';
import { loadApi, loadPoint, buildFrames } from './api.js';
import {
  initMap, setBounds, showFrame, clearOverlay, setOpacity, setMapStyle, sampleAt,
} from './map.js';
import { createPlayer } from './player.js';
import { initCharts, updateCharts } from './charts.js';

// ── Stan ──────────────────────────────────────────────────────────────────────
let apiData       = null;
let activeProduct = 'det';     // aktywny typ prognozy (klucz produktu fcst)
let availableTypes = [];       // typy obecne w danych (det / icon …)
let opacity       = clampInt(localStorage.getItem(LS_OPACITY), DEFAULT_OPACITY) / 100;
let speedIdx      = clampInt(localStorage.getItem(LS_SPEED), DEFAULT_SPEED_IDX);
let map           = null;
let player        = null;
let clickMarker   = null;

function clampInt(v, def) { const n = parseInt(v, 10); return Number.isFinite(n) ? n : def; }

// ── DOM ───────────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const loadingEl   = $('loading');
const timeClock   = $('time-clock');
const timeDate    = $('time-date');
const fcBadge     = $('forecast-badge');
const infoTime    = $('info-time');
const infoValue   = $('info-value');
const infoHint    = $('info-hint');
const frameLabel  = $('frame-label');
const frameBadge  = $('frame-badge');
const tickStart   = $('tick-start');
const tickNow     = $('tick-now');
const tickEnd     = $('tick-end');
const sidePanel   = $('side-panel');
const spTitle     = $('sp-title');

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  initSettings();
  initSidePanel();
  initCharts();

  map = initMap();
  player = createPlayer({ onFrame: onFrame, onClear: onClear });
  player.setSpeed(SPEED_STEPS[speedIdx]);

  map.on('mousemove', onHover);
  map.on('mouseout', resetInfo);
  map.on('click', onMapClick);

  try {
    apiData = await loadApi();
    if (!apiData.bounds) { frameLabel.textContent = 'Brak danych'; return; }
    setBounds(apiData.bounds);
    detectTypes();
    applyProduct(true);
  } catch (e) {
    console.error(e);
    frameLabel.textContent = 'Błąd API';
  } finally {
    loadingEl.classList.add('hidden');
  }

  setInterval(refresh, REFRESH_MS);
}

// ── Typy prognozy (det / icon) ────────────────────────────────────────────────
function detectTypes() {
  availableTypes = FORECAST_TYPES.filter(t => (apiData.fcst?.[t.key]?.length));
  if (!availableTypes.length) availableTypes = [FORECAST_TYPES[0]];   // fallback: det

  // domyślny typ: meta.default jeśli dostępny, inaczej pierwszy obecny
  const def = apiData.meta?.default;
  activeProduct = availableTypes.some(t => t.key === def)
    ? def : availableTypes[0].key;

  buildTypeToggle();
}

function buildTypeToggle() {
  const el = $('type-toggle');
  el.innerHTML = '';
  if (availableTypes.length < 2) { el.classList.add('hidden'); return; }

  availableTypes.forEach(t => {
    const b = document.createElement('button');
    b.className = 'seg-btn' + (t.key === activeProduct ? ' active' : '');
    b.dataset.key = t.key;
    b.textContent = t.short;
    b.title = t.label;
    b.addEventListener('click', () => switchType(t.key));
    el.appendChild(b);
  });
  el.classList.remove('hidden');
}

function switchType(key) {
  if (key === activeProduct) return;
  activeProduct = key;
  $('type-toggle').querySelectorAll('.seg-btn')
    .forEach(b => b.classList.toggle('active', b.dataset.key === key));
  buildColorbar(key);
  if ($('sp-info').classList.contains('active')) updateInfoPage();
  // updateFrames zachowuje pozycję na osi czasu (det i icon mają te same kroki)
  player.updateFrames(buildFrames(apiData, key));
}

function applyProduct(initial = false) {
  buildColorbar(activeProduct);
  if ($('sp-info').classList.contains('active')) updateInfoPage();
  const frames = buildFrames(apiData, activeProduct);
  if (initial) player.loadFrames(frames); else player.updateFrames(frames);
}

// ── Callback klatki ───────────────────────────────────────────────────────────
function onFrame(frame, idx, total) {
  showFrame(frame, opacity);
  frameLabel.textContent = frame.label;
  frameBadge.innerHTML = frame.is_forecast
    ? '<span class="badge badge-fcst">Prognoza</span>'
    : '<span class="badge badge-obs">Obserwacja</span>';
  updateClock(frame);
  updateTicks(idx, total);
  resetInfo();
}

function onClear() {
  clearOverlay();
  frameLabel.textContent = 'Brak danych';
  frameBadge.innerHTML = '';
}

function updateClock(frame) {
  const dt = new Date(frame.time.includes('Z') ? frame.time : frame.time + 'Z');
  timeClock.textContent = dt.toLocaleTimeString('pl-PL', { timeZone: 'Europe/Warsaw', hour: '2-digit', minute: '2-digit' });
  timeDate.textContent  = dt.toLocaleDateString('pl-PL', { timeZone: 'Europe/Warsaw', day: '2-digit', month: '2-digit', year: 'numeric' });
  fcBadge.classList.toggle('hidden', !frame.is_forecast);
}

function updateTicks(idx, total) {
  const frames = buildFrames(apiData, activeProduct);
  if (!frames.length) return;
  tickStart.textContent = frames[0].label;
  tickEnd.textContent   = frames[frames.length - 1].label;
  const fcStart = frames.findIndex(f => f.is_forecast);
  tickNow.textContent = fcStart > 0 ? '▶ ' + frames[fcStart].label : '';
}

// ── Pasek kolorów ─────────────────────────────────────────────────────────────
function buildColorbar(key) {
  const pal = PALETTES[key];
  const bar = $('colorbar-bar');
  const ticks = $('colorbar-ticks');
  const n = pal.entries.length;

  const stops = [];
  pal.entries.forEach((e, i) => {
    const c = `rgb(${e.r},${e.g},${e.b})`;
    stops.push(`${c} ${(i / n * 100).toFixed(2)}%`, `${c} ${((i + 1) / n * 100).toFixed(2)}%`);
  });
  bar.style.background = `linear-gradient(to right, ${stops.join(',')})`;
  $('colorbar-label').textContent = pal.unit;

  // Etykiety osi: użyj predefiniowanych ticków palety, w przeciwnym razie
  // wygeneruj po jednym na kubełek (palety prob).
  const tickList = pal.ticks
    ?? pal.entries.map((e, i) => ({ pos: i / n * 100, label: e.label ?? '' }))
         .concat([{ pos: 100, label: pal.unit === '%' ? '100' : '' }]);

  ticks.innerHTML = '';
  tickList.forEach(t => {
    const s = document.createElement('span');
    s.className = 'cb-tick';
    s.style.left = t.pos + '%';
    if (t.pos <= 0)        s.style.transform = 'none';
    else if (t.pos >= 100) s.style.transform = 'translateX(-100%)';
    s.textContent = t.label;
    ticks.appendChild(s);
  });
}

// Kompaktowy format wartości natężenia opadu
function fmtRate(v) {
  if (v >= 10)  return Math.round(v).toString();
  if (v >= 1)   return v.toFixed(1);
  if (v >= 0.1) return v.toFixed(2);
  return v.toFixed(3);
}

// ── Hover → odczyt wartości ───────────────────────────────────────────────────
function onHover(e) {
  const f = player.currentFrame;
  if (!f) return;
  infoTime.textContent = f.label;
  const s = sampleAt(e.latlng.lat, e.latlng.lng);
  if (!s) return;
  if (s.outside) {
    infoValue.textContent = '–';
    infoValue.style.color = '#6b7f9c';
    infoHint.textContent = 'poza obszarem radaru';
    return;
  }
  const pal = PALETTES[activeProduct];
  if (s.a < 15) {
    infoValue.textContent = pal.unit === '%' ? '< 10%' : '0 mm/h';
    infoValue.style.color = '#6b7f9c';
    infoHint.textContent = 'poniżej progu';
  } else {
    const entry = colorToEntry(pal, s.r, s.g, s.b);
    if (entry) {
      let range;
      if (pal.unit === '%') {
        range = entry.hi === Infinity ? `> ${entry.lo}` : `${entry.lo}–${entry.hi}`;
      } else {
        range = entry.hi === Infinity ? `> ${fmtRate(entry.lo)}` : `${fmtRate(entry.lo)}–${fmtRate(entry.hi)}`;
      }
      infoValue.textContent = `${range} ${pal.unit}`;
      infoValue.style.color = `rgb(${entry.r},${entry.g},${entry.b})`;
    }
    infoHint.textContent = '';
  }
}

function resetInfo() {
  const f = player.currentFrame;
  infoTime.textContent = f ? f.label : '–';
  infoValue.textContent = '–';
  infoValue.style.color = 'var(--accent)';
  infoHint.textContent = 'najedź na mapę';
}

// ── Kliknięcie → panel danych punktowych ──────────────────────────────────────
async function onMapClick(e) {
  const { lat, lng } = e.latlng;
  if (clickMarker) map.removeLayer(clickMarker);
  clickMarker = L.marker([lat, lng]).addTo(map);

  openPanel('point');
  $('sp-coords').innerHTML = `📍 <strong>${lat.toFixed(4)}°N, ${lng.toFixed(4)}°E</strong>`;
  $('sp-loading').style.display = 'block';
  $('sp-charts').style.display = 'none';
  $('sp-point-hint').style.display = 'none';

  try {
    const data = await loadPoint(lat, lng);
    if (data.error) throw new Error(data.error);
    $('sp-coords').innerHTML = `📍 <strong>${data.lat.toFixed(4)}°N, ${data.lng.toFixed(4)}°E</strong>`;
    $('sp-loading').style.display = 'none';
    $('sp-charts').style.display = 'block';
    updateCharts(data);
  } catch (err) {
    console.error(err);
    $('sp-loading').style.display = 'none';
    $('sp-point-hint').style.display = 'block';
    $('sp-point-hint').textContent = '⚠ Błąd pobierania danych punktu';
  }
}

// ── Panel boczny ──────────────────────────────────────────────────────────────
const SP_PAGES = { point: 'sp-point', settings: 'sp-settings', info: 'sp-info' };
const SP_TITLES = { point: 'Dane punktu', settings: 'Ustawienia', info: 'O produkcie' };

function openPanel(name) {
  Object.entries(SP_PAGES).forEach(([k, id]) => $(id).classList.toggle('active', k === name));
  spTitle.textContent = SP_TITLES[name];
  sidePanel.classList.add('open');
  document.querySelectorAll('.side-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.panel === name));
  if (name === 'info') updateInfoPage();
}

function closePanel() {
  sidePanel.classList.remove('open');
  document.querySelectorAll('.side-btn').forEach(b => b.classList.remove('active'));
  if (clickMarker) { map.removeLayer(clickMarker); clickMarker = null; }
}

function initSidePanel() {
  $('sp-close').addEventListener('click', closePanel);
  document.querySelectorAll('.side-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.panel;
      const open = sidePanel.classList.contains('open')
        && document.querySelector('.side-btn.active')?.dataset.panel === name;
      open ? closePanel() : openPanel(name);
    });
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanel(); });
}

function updateInfoPage() {
  const p = productByKey(activeProduct);
  $('sp-info-name').textContent = p.label;
  $('sp-info-desc').textContent = p.desc;
}

// ── Ustawienia ────────────────────────────────────────────────────────────────
function initSettings() {
  const opSl = $('opacity-slider'), opVal = $('opacity-val');
  opSl.value = Math.round(opacity * 100);
  opVal.textContent = opSl.value + '%';
  opSl.addEventListener('input', () => {
    opacity = parseInt(opSl.value, 10) / 100;
    opVal.textContent = opSl.value + '%';
    setOpacity(opacity);
    localStorage.setItem(LS_OPACITY, opSl.value);
  });

  const spSl = $('speed-slider');
  spSl.value = speedIdx;
  spSl.addEventListener('input', () => {
    speedIdx = parseInt(spSl.value, 10);
    player?.setSpeed(SPEED_STEPS[speedIdx]);
    localStorage.setItem(LS_SPEED, speedIdx);
  });

  const curStyle = localStorage.getItem(LS_STYLE) || 'dark';
  document.querySelectorAll('.map-style-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.style === curStyle);
    btn.addEventListener('click', () => {
      setMapStyle(btn.dataset.style);
      document.querySelectorAll('.map-style-btn').forEach(b => b.classList.toggle('active', b === btn));
    });
  });
}

// ── Auto-odświeżanie ──────────────────────────────────────────────────────────
async function refresh() {
  try {
    const fresh = await loadApi();
    if (!fresh.bounds) return;
    apiData = fresh;
    // Ponowna detekcja typów (np. ICON pojawił się / zniknął). Zachowaj bieżący
    // typ jeśli nadal dostępny, inaczej przeskocz na pierwszy obecny.
    const prev = activeProduct;
    detectTypes();
    if (availableTypes.some(t => t.key === prev)) {
      activeProduct = prev;
      buildTypeToggle();
    }
    buildColorbar(activeProduct);
    player.updateFrames(buildFrames(apiData, activeProduct));
  } catch (_) { /* cicho — spróbujemy ponownie */ }
}

init();
