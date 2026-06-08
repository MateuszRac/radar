// ── Mapa Leaflet: podkład, overlay radarowy, maska zasięgu, próbkowanie piksela ─
import { MAP_CENTER, MAP_ZOOM, LS_STYLE } from './config.js';

const TILE_STYLES = {
  dark: {
    base:   'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png',
    labels: 'https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png',
  },
  light: {
    base:   'https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png',
    labels: 'https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}{r}.png',
  },
};
const ATTRIB = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
             + '© <a href="https://carto.com/attributions">CARTO</a> · Radar: IMGW · Prognoza: STEPS';

const R_MERC = 6378137;
function toMerc(lat, lng) {
  return [
    lng * Math.PI / 180 * R_MERC,
    Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)) * R_MERC,
  ];
}

let _map = null, _tiles = null, _labels = null;
let _overlay = null, _mask = null;
let _bounds = null, _mercSW = null, _mercNE = null;

// Ukryty canvas do odczytu koloru piksela aktualnej klatki
const _canvas = document.createElement('canvas');
const _ctx = _canvas.getContext('2d', { willReadFrequently: true });
let _canvasReady = false;

export function initMap() {
  _map = L.map('map', {
    center: MAP_CENTER, zoom: MAP_ZOOM, zoomControl: true,
    minZoom: 5, maxBounds: [[47.5, 9.5], [57.5, 27.5]], maxBoundsViscosity: 1.0,
  });
  _map.createPane('labelsPane');
  _map.getPane('labelsPane').style.zIndex = 450;
  _map.getPane('labelsPane').style.pointerEvents = 'none';
  applyStyle(localStorage.getItem(LS_STYLE) || 'dark');
  return _map;
}

function applyStyle(style) {
  const t = TILE_STYLES[style] ?? TILE_STYLES.dark;
  if (_tiles)  _tiles.remove();
  if (_labels) _labels.remove();
  const opts = { attribution: ATTRIB, subdomains: 'abcd', maxZoom: 19 };
  _tiles  = L.tileLayer(t.base, opts).addTo(_map);
  _labels = L.tileLayer(t.labels, { ...opts, pane: 'labelsPane' }).addTo(_map);
}

export function setMapStyle(style) {
  localStorage.setItem(LS_STYLE, style);
  applyStyle(style);
}

/** Ustawia obwiednię obrazów (raz, z bounds.json) i dopasowuje widok. */
export function setBounds(bounds) {
  _bounds = [bounds.sw, bounds.ne];
  _mercSW = toMerc(bounds.sw[0], bounds.sw[1]);
  _mercNE = toMerc(bounds.ne[0], bounds.ne[1]);
  _map.fitBounds(_bounds, { padding: [24, 24] });
  _updateMask();
}

/** Wyświetla klatkę: aktualizuje overlay i wczytuje piksele do próbkowania. */
export function showFrame(frame, opacity) {
  if (!_bounds) return;
  const ll = L.latLngBounds(_bounds[0], _bounds[1]);
  if (_overlay) {
    _overlay.setUrl(frame.url);
    _overlay.setOpacity(opacity);
  } else {
    _overlay = L.imageOverlay(frame.url, ll, { opacity, interactive: false }).addTo(_map);
  }
  _preloadCanvas(frame.url);
}

export function clearOverlay() {
  if (_overlay) { _overlay.remove(); _overlay = null; }
}

export function setOpacity(opacity) {
  if (_overlay) _overlay.setOpacity(opacity);
}

function _preloadCanvas(url) {
  _canvasReady = false;
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    _canvas.width = img.naturalWidth;
    _canvas.height = img.naturalHeight;
    _ctx.clearRect(0, 0, _canvas.width, _canvas.height);
    _ctx.drawImage(img, 0, 0);
    _canvasReady = true;
  };
  img.src = url;
}

/**
 * Próbkuje kolor aktualnej klatki w punkcie geo.
 * Zwraca { r, g, b, a } albo null gdy poza obszarem / brak danych.
 */
export function sampleAt(lat, lng) {
  if (!_canvasReady || !_bounds) return null;
  const [sw, ne] = _bounds;
  if (lat < sw[0] || lat > ne[0] || lng < sw[1] || lng > ne[1]) return { outside: true };
  const [cx, cy] = toMerc(lat, lng);
  const px = Math.round((cx - _mercSW[0]) / (_mercNE[0] - _mercSW[0]) * (_canvas.width - 1));
  const py = Math.round((1 - (cy - _mercSW[1]) / (_mercNE[1] - _mercSW[1])) * (_canvas.height - 1));
  if (px < 0 || py < 0 || px >= _canvas.width || py >= _canvas.height) return null;
  const [r, g, b, a] = _ctx.getImageData(px, py, 1, 1).data;
  return { r, g, b, a };
}

// ── Maska zasięgu (przyciemnia obszar poza radarem) ───────────────────────────
function _updateMask() {
  if (!_bounds) return;
  const [sw, ne] = _bounds;
  const outer = [[90, -180], [90, 180], [-90, 180], [-90, -180]];
  const inner = [[sw[0], sw[1]], [sw[0], ne[1]], [ne[0], ne[1]], [ne[0], sw[1]], [sw[0], sw[1]]];
  if (_mask) {
    _mask.setLatLngs([outer, inner]);
  } else {
    _mask = L.polygon([outer, inner], {
      fillColor: '#05080f', fillOpacity: 0.5, stroke: false, interactive: false,
    }).addTo(_map);
  }
}
