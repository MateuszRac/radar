// ── Konfiguracja i stałe ──────────────────────────────────────────────────────

export const API_URL    = 'index.php?api=1';
export const POINT_URL  = 'point_data.php';

export const MAP_CENTER = [52.1, 19.5];
export const MAP_ZOOM   = 6;

// Prędkości animacji [ms/klatkę] — indeksowane suwakiem 0..6 (wyższy = szybciej)
export const SPEED_STEPS       = [1500, 1000, 700, 450, 300, 180, 100];
export const DEFAULT_SPEED_IDX = 4;        // → 300 ms (trochę szybciej niż wcześniej)
export const END_PAUSE_MS      = 1000;     // pauza na końcu pętli
export const DEFAULT_OPACITY   = 82;       // % krycia warstwy radarowej
export const REFRESH_MS        = 5 * 60 * 1000;  // auto-odświeżanie API

// klucze localStorage
export const LS_OPACITY = 'steps_opacity';
export const LS_SPEED   = 'steps_speed';
export const LS_STYLE   = 'steps_map_style';

// ── Typy prognozy (przełącznik w toolbarze, gdy dostępne ≥2) ──────────────────
// Klucz = produkt fcst zwracany przez ?api=1. Kolejność = kolejność przycisków.
export const FORECAST_TYPES = [
  { key: 'det',       label: 'S-PROG',           short: 'S-PROG' },
  { key: 'linda',     label: 'LINDA',            short: 'LINDA' },
  { key: 'anvil',     label: 'ANVIL',            short: 'ANVIL' },
  { key: 'icon',      label: 'S-PROG + ICON-EU', short: 'S-PROG+ICON' },
  { key: 'lindaicon', label: 'LINDA + ICON-EU',  short: 'LINDA+ICON' },
  { key: 'anvilicon', label: 'ANVIL + ICON-EU',  short: 'ANVIL+ICON' },
];

// ── Produkty (opisy do panelu „O produkcie”) ──────────────────────────────────
export const PRODUCTS = [
  {
    key: 'mean', label: 'Ensemble mean', short: 'Średnia', unit: 'mm/h',
    desc: 'Średnia z ensemblu STEPS — oczekiwane natężenie opadu. '
        + 'Wygładza niepewność, dobra do ogólnego obrazu rozwoju opadu.',
  },
  {
    key: 'det', label: 'S-PROG (det.)', short: 'S-PROG', unit: 'mm/h',
    desc: 'Deterministyczna ekstrapolacja S-PROG z progresywnym wygładzaniem '
        + 'nieprzewidywalnych małych skal — „najlepsza pojedyncza prognoza”.',
  },
  {
    key: 'linda', label: 'LINDA', short: 'LINDA', unit: 'mm/h',
    desc: 'LINDA (Lagrangian INtegro-Difference equation model with Autoregression) — '
        + 'deterministyczna prognoza oparta na cechach (komórkach opadu) z modelem '
        + 'autoregresyjnym; lepiej zachowuje lokalne maksima konwekcyjne niż S-PROG.',
  },
  {
    key: 'anvil', label: 'ANVIL', short: 'ANVIL', unit: 'mm/h',
    desc: 'ANVIL (Autoregressive Nowcasting using VIL) — deterministyczna prognoza '
        + 'z modelem autoregresyjnym AR(2) w przestrzennie zdekomponowanym polu; '
        + 'oddaje rozwój i zanik komórek opadu lepiej niż czysta ekstrapolacja.',
  },
  {
    key: 'icon', label: 'S-PROG + ICON-EU', short: 'S-PROG+ICON', unit: 'mm/h',
    desc: 'S-PROG zmieszany liniowo z modelem ICON-EU (DWD): radar dominuje na '
        + 'krótkich horyzontach, model numeryczny na dłuższych (0%→100% w horyzoncie).',
  },
  {
    key: 'lindaicon', label: 'LINDA + ICON-EU', short: 'LINDA+ICON', unit: 'mm/h',
    desc: 'LINDA zmieszana liniowo z modelem ICON-EU (DWD): nowcasting cech opadu '
        + 'na krótkich horyzontach, tło mezoskalowe NWP na dłuższych.',
  },
  {
    key: 'anvilicon', label: 'ANVIL + ICON-EU', short: 'ANVIL+ICON', unit: 'mm/h',
    desc: 'ANVIL zmieszany liniowo z modelem ICON-EU (DWD): autoregresyjny nowcasting '
        + 'na krótkich horyzontach, tło mezoskalowe NWP na dłuższych.',
  },
  {
    key: 'prob01', label: 'P > 0.1 mm/h', short: 'P>0.1', unit: '%',
    desc: 'Prawdopodobieństwo wystąpienia jakiegokolwiek opadu (> 0.1 mm/h), '
        + 'liczone z rozrzutu członków ensemblu.',
  },
  {
    key: 'prob10', label: 'P > 10 mm/h', short: 'P>10', unit: '%',
    desc: 'Prawdopodobieństwo intensywnego opadu (> 10 mm/h), '
        + 'liczone z rozrzutu członków ensemblu.',
  },
];

// ── Palety (muszą być zgodne z radar/palette.py i point_data.php) ─────────────
// RATE — paleta natężenia opadu z radar/palette.py (quantity == "RATE"):
// 23 kolory, progi log-spaced 10**linspace(-2, 1.8, 24) ≈ 0.01–63 mm/h.
const RATE_ENTRIES = [
  { r: 212, g: 240, b: 255, lo:   0.0100, hi:   0.0146 },
  { r: 160, g: 216, b: 240, lo:   0.0146, hi:   0.0214 },
  { r: 112, g: 192, b: 232, lo:   0.0214, hi:   0.0313 },
  { r:  64, g: 168, b: 224, lo:   0.0313, hi:   0.0458 },
  { r:  30, g: 144, b: 216, lo:   0.0458, hi:   0.0670 },
  { r:   0, g: 200, b: 160, lo:   0.0670, hi:   0.0980 },
  { r:  64, g: 224, b:  96, lo:   0.0980, hi:   0.1434 },
  { r: 160, g: 240, b:   0, lo:   0.1434, hi:   0.2098 },
  { r: 255, g: 255, b:   0, lo:   0.2098, hi:   0.3069 },
  { r: 255, g: 208, b:   0, lo:   0.3069, hi:   0.4489 },
  { r: 255, g: 153, b:   0, lo:   0.4489, hi:   0.6567 },
  { r: 255, g: 102, b:   0, lo:   0.6567, hi:   0.9607 },
  { r: 255, g:  51, b:   0, lo:   0.9607, hi:   1.4055 },
  { r: 224, g:   0, b:   0, lo:   1.4055, hi:   2.0561 },
  { r: 176, g:   0, b:   0, lo:   2.0561, hi:   3.0079 },
  { r: 128, g:   0, b:   0, lo:   3.0079, hi:   4.4003 },
  { r: 192, g:   0, b: 192, lo:   4.4003, hi:   6.4372 },
  { r: 153, g:   0, b: 204, lo:   6.4372, hi:   9.4170 },
  { r: 102, g:   0, b: 187, lo:   9.4170, hi:  13.7762 },
  { r: 212, g: 176, b: 240, lo:  13.7762, hi:  20.1534 },
  { r: 232, g: 208, b: 248, lo:  20.1534, hi:  29.4826 },
  { r: 200, g: 200, b: 200, lo:  29.4826, hi:  43.1303 },
  { r: 144, g: 144, b: 144, lo:  43.1303, hi:  63.0957 },
];

// Etykiety osi paska kolorów — wartości "okrągłe" rzutowane na pozycję kubełka.
const RATE_TICKS = [
  { pos:   0.0, label: '0.01' },
  { pos:  26.1, label: '0.1'  },
  { pos:  43.5, label: '0.5'  },
  { pos:  52.2, label: '1'    },
  { pos:  60.9, label: '2'    },
  { pos:  69.6, label: '5'    },
  { pos:  78.3, label: '10'   },
  { pos:  87.0, label: '20'   },
  { pos:  95.7, label: '50'   },
  { pos: 100.0, label: '63'   },
];

const PROB01_ENTRIES = [
  { r: 222, g: 235, b: 247, lo: 10,  hi: 20,  label: '10' },
  { r: 198, g: 219, b: 239, lo: 20,  hi: 30,  label: '20' },
  { r: 158, g: 202, b: 225, lo: 30,  hi: 40,  label: '30' },
  { r: 107, g: 174, b: 214, lo: 40,  hi: 50,  label: '40' },
  { r:  66, g: 146, b: 198, lo: 50,  hi: 60,  label: '50' },
  { r:  33, g: 113, b: 181, lo: 60,  hi: 70,  label: '60' },
  { r:   8, g:  81, b: 156, lo: 70,  hi: 80,  label: '70' },
  { r:   8, g:  48, b: 107, lo: 80,  hi: 90,  label: '80' },
  { r:   4, g:  30, b:  66, lo: 90,  hi: 100, label: '90' },
];

const PROB10_ENTRIES = [
  { r: 254, g: 224, b: 210, lo: 10,  hi: 20,  label: '10' },
  { r: 252, g: 187, b: 161, lo: 20,  hi: 30,  label: '20' },
  { r: 252, g: 146, b: 114, lo: 30,  hi: 40,  label: '30' },
  { r: 251, g: 106, b:  74, lo: 40,  hi: 50,  label: '40' },
  { r: 239, g:  59, b:  44, lo: 50,  hi: 60,  label: '50' },
  { r: 203, g:  24, b:  29, lo: 60,  hi: 70,  label: '60' },
  { r: 165, g:  15, b:  21, lo: 70,  hi: 80,  label: '70' },
  { r: 103, g:   0, b:  13, lo: 80,  hi: 90,  label: '80' },
  { r:  63, g:   0, b:   7, lo: 90,  hi: 100, label: '90' },
];

const RATE_PAL = { title: 'Natężenie opadu', unit: 'mm/h', entries: RATE_ENTRIES, ticks: RATE_TICKS };
export const PALETTES = {
  mean:      RATE_PAL,
  det:       RATE_PAL,
  linda:     RATE_PAL,
  anvil:     RATE_PAL,
  icon:      RATE_PAL,
  lindaicon: RATE_PAL,
  anvilicon: RATE_PAL,
  prob01: { title: 'P(R > 0.1 mm/h)', unit: '%', entries: PROB01_ENTRIES },
  prob10: { title: 'P(R > 10 mm/h)', unit: '%', entries: PROB10_ENTRIES },
};

/** Dopasowuje kolor piksela do wpisu palety (najbliższy w przestrzeni RGB). */
export function colorToEntry(palette, r, g, b) {
  let best = null, minD = Infinity;
  for (const e of palette.entries) {
    const d = (r - e.r) ** 2 + (g - e.g) ** 2 + (b - e.b) ** 2;
    if (d < minD) { minD = d; best = e; }
  }
  return minD < 12000 ? best : null;
}

export function productByKey(key) {
  return PRODUCTS.find(p => p.key === key) ?? PRODUCTS[0];
}
