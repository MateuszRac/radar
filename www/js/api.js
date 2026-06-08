// ── Pobieranie danych i budowa klatek ─────────────────────────────────────────
import { API_URL, POINT_URL } from './config.js';

/** Pobiera manifest klatek z PHP (?api=1). */
export async function loadApi() {
  const r = await fetch(API_URL + '&t=' + Date.now(), { cache: 'no-store' });
  if (!r.ok) throw new Error('API HTTP ' + r.status);
  return r.json();
}

/** Pobiera szereg czasowy dla punktu (kliknięcie na mapie). */
export async function loadPoint(lat, lng) {
  const r = await fetch(`${POINT_URL}?lat=${lat}&lng=${lng}`, { cache: 'no-store' });
  if (!r.ok) throw new Error('point HTTP ' + r.status);
  return r.json();
}

/**
 * Buduje listę klatek dla wybranego produktu: obserwacje + prognoza.
 * Każda klatka: { url, label, time, is_forecast }.
 */
export function buildFrames(apiData, product) {
  if (!apiData) return [];
  const obs = (apiData.obs ?? []).map(f => ({
    url: f.url, label: f.label, time: f.time, is_forecast: false,
  }));
  const fcst = (apiData.fcst?.[product] ?? []).map(f => ({
    url: f.url, label: f.label, time: f.time, is_forecast: true,
  }));
  return [...obs, ...fcst];
}
