// ── Meteogram danych punktowych (panel boczny) ────────────────────────────────
// Natężenie opadu [mm/h]: obserwacje (niebieskie słupki) + prognoza (pomarańczowe
// słupki). Pokazujemy jedną serię prognozy (domyślnie ANVIL); jeśli aktywny typ
// nie ma danych w punkcie, bierzemy pierwszy dostępny.
// Wymaga globalnego Chart.js (ładowanego w index.php).

let chartRain = null;

const OBS_COLOR  = 'rgba(29,111,224,.85)';   // niebieski  — obserwacje
const FCST_COLOR = 'rgba(224,112,32,.92)';   // pomarańczowy — prognoza

// Kolejność prób przy wyborze serii prognozy (gdy aktywny produkt nie ma danych).
const FCST_KEYS = ['anvil', 'det', 'linda', 'icon', 'lindaicon', 'anvilicon'];

const _p2 = n => String(n).padStart(2, '0');

function fmtTimeUTC(iso) {
  const d = new Date(iso);
  return `${_p2(d.getUTCHours())}:${_p2(d.getUTCMinutes())}`;
}

function fmtFullUTC(iso) {
  const d = new Date(iso);
  return `${d.getUTCFullYear()}-${_p2(d.getUTCMonth() + 1)}-${_p2(d.getUTCDate())} `
       + `${_p2(d.getUTCHours())}:${_p2(d.getUTCMinutes())} UTC`;
}

const BASE = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 250 },
  plugins: {
    legend: { display: true, position: 'top',
      labels: { color: '#90aad0', font: { size: 9 }, boxWidth: 10, boxHeight: 10, padding: 6 } },
    tooltip: {
      callbacks: {
        // Tytuł tooltipa = pełna data i godzina (UTC) danego słupka.
        title: (items) => {
          const t = chartRain?._fullTimes?.[items[0].dataIndex];
          return t ? fmtFullUTC(t) : items[0].label;
        },
      },
    },
  },
  scales: {
    x: { grid: { color: 'rgba(45,68,112,.4)' },
         ticks: { color: '#90aad0', maxRotation: 0, autoSkip: true, font: { size: 9 } } },
    y: { grid: { color: 'rgba(45,68,112,.4)' }, ticks: { color: '#90aad0', font: { size: 9 } },
         beginAtZero: true, title: { display: true, text: 'mm/h', color: '#6b7f9c', font: { size: 9 } } },
  },
};

export function initCharts() {
  Chart.defaults.color = '#90aad0';
  Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";

  const datasets = [
    { type: 'bar', label: 'Obserwacje', backgroundColor: OBS_COLOR,  data: [] },
    { type: 'bar', label: 'Prognoza',   backgroundColor: FCST_COLOR, data: [] },
  ];

  chartRain = new Chart(document.getElementById('chart-rain'), {
    type: 'bar',
    data: { labels: [], datasets },
    options: BASE,
  });
}

export function updateCharts(data, product = 'anvil') {
  // Wybór serii prognozy: aktywny produkt jeśli ma wartości, inaczej pierwszy dostępny.
  let pkey = product;
  if (!data.fcst.some(f => f[pkey] != null)) {
    pkey = FCST_KEYS.find(k => data.fcst.some(f => f[k] != null)) || product;
  }

  const obsLabels  = data.obs.map(o => o.label);                       // "14:25"
  const obsValues  = data.obs.map(o => o.value);
  // Etykieta prognozy: dwie linie — lead ("+15 min") oraz godzina ważności.
  const fcstLabels = data.fcst.map(f => [`+${f.lead_min} min`, fmtTimeUTC(f.time)]);
  const fcstValues = data.fcst.map(f => f[pkey] ?? null);

  const n = obsLabels.length;
  chartRain.data.labels = [...obsLabels, ...fcstLabels];
  chartRain.data.datasets[0].data = [...obsValues, ...Array(fcstValues.length).fill(null)];
  chartRain.data.datasets[1].data = [...Array(n).fill(null), ...fcstValues];

  // Pełne czasy (UTC) dla tooltipów — wyrównane do etykiet.
  chartRain._fullTimes = [...data.obs.map(o => o.time), ...data.fcst.map(f => f.time)];

  chartRain.update();
}
