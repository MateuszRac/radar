// ── Meteogram danych punktowych (panel boczny) ────────────────────────────────
// Natężenie opadu [mm/h]: obserwacje (słupki) + linia dla każdego dostępnego
// typu prognozy (S-PROG, LINDA, oraz warianty z ICON). Serie bez danych są ukryte.
// Wymaga globalnego Chart.js (ładowanego w index.php).

let chartRain = null;

// Kolejność i kolory serii prognostycznych (klucze = produkty z ?api=1 / point_data).
const FCST_SERIES = [
  { key: 'det',       label: 'S-PROG',      color: '#e07020' },
  { key: 'linda',     label: 'LINDA',       color: '#27ae60' },
  { key: 'icon',      label: 'S-PROG+ICON', color: '#9b59b6' },
  { key: 'lindaicon', label: 'LINDA+ICON',  color: '#16a8a8' },
];

const BASE = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 250 },
  plugins: {
    legend: { display: true, position: 'top',
      labels: { color: '#90aad0', font: { size: 9 }, boxWidth: 10, boxHeight: 10, padding: 6 } },
  },
  scales: {
    x: { grid: { color: 'rgba(45,68,112,.4)' }, ticks: { color: '#90aad0', maxRotation: 45, font: { size: 9 } } },
    y: { grid: { color: 'rgba(45,68,112,.4)' }, ticks: { color: '#90aad0', font: { size: 9 } },
         beginAtZero: true, title: { display: true, text: 'mm/h', color: '#6b7f9c', font: { size: 9 } } },
  },
};

export function initCharts() {
  Chart.defaults.color = '#90aad0';
  Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";

  const datasets = [
    { type: 'bar', label: 'Obserwacje', backgroundColor: 'rgba(29,111,224,.85)', data: [] },
    ...FCST_SERIES.map(s => ({
      type: 'line', label: s.label,
      borderColor: s.color, backgroundColor: s.color,
      data: [], fill: false, tension: 0.25, borderWidth: 2,
      pointRadius: 2, spanGaps: false, hidden: true,
    })),
  ];

  chartRain = new Chart(document.getElementById('chart-rain'), {
    type: 'bar',
    data: { labels: [], datasets },
    options: BASE,
  });
}

export function updateCharts(data) {
  const obsLabels  = data.obs.map(o => o.label);
  const obsValues  = data.obs.map(o => o.value);
  const fcstLabels = data.fcst.map(f => f.label);
  const padFront = arr => [...Array(obsLabels.length).fill(null), ...arr];

  chartRain.data.labels = [...obsLabels, ...fcstLabels];
  chartRain.data.datasets[0].data = [...obsValues, ...Array(fcstLabels.length).fill(null)];

  FCST_SERIES.forEach((s, i) => {
    const vals = data.fcst.map(f => f[s.key] ?? null);
    chartRain.data.datasets[i + 1].data = padFront(vals);
    chartRain.setDatasetVisibility(i + 1, vals.some(v => v !== null));
  });

  chartRain.update();
}
