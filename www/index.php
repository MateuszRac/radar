<?php
// ── API listy klatek ──────────────────────────────────────────────────────────
if (isset($_GET['api'])) {
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');

    $dir  = __DIR__ . '/data/';

    // ── Ścieżka główna: manifest.json (atomowy, spójny zestaw plików) ─────────
    $manifest_file = $dir . 'manifest.json';
    if (is_file($manifest_file)) {
        $man = json_decode(file_get_contents($manifest_file), true);
        if (is_array($man)) {
            $mapEntry = function ($e) {
                $out = ['url' => 'data/' . $e['file'], 'time' => $e['time'], 'label' => $e['label']];
                if (isset($e['lead_min'])) $out['lead_min'] = $e['lead_min'];
                return $out;
            };
            $obs  = array_map($mapEntry, $man['obs'] ?? []);
            $fcst = [];
            foreach (($man['fcst'] ?? []) as $key => $arr) {
                $fcst[$key] = array_map($mapEntry, $arr);
            }
            echo json_encode([
                'obs'    => $obs,
                'fcst'   => $fcst,
                'bounds' => $man['bounds'] ?? null,
                'meta'   => $man['meta'] ?? null,
            ]);
            exit;
        }
    }

    // ── Fallback: skanowanie katalogu (gdy brak manifestu) ───────────────────
    $obs  = [];
    $fcst = ['det' => [], 'icon' => [], 'mean' => [], 'prob01' => [], 'prob10' => []];

    foreach (glob($dir . '*.png') as $f) {
        $name = basename($f, '.png');

        if (preg_match('/^obs_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})$/', $name, $m)) {
            $obs[] = [
                'url'   => 'data/' . basename($f),
                'time'  => "{$m[1]}-{$m[2]}-{$m[3]}T{$m[4]}:{$m[5]}:00Z",
                'label' => "{$m[4]}:{$m[5]} UTC",
                'sort'  => $name,
            ];
        } elseif (preg_match(
            '/^fcst_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})_plus_(\d+)_(mean|det|icon|prob01|prob10)$/',
            $name, $m
        )) {
            $lead     = (int)$m[6];
            $product  = $m[7];
            $base_ts  = gmmktime((int)$m[4], (int)$m[5], 0, (int)$m[2], (int)$m[3], (int)$m[1]);
            $valid_ts = $base_ts + $lead * 60;
            $fcst[$product][] = [
                'url'      => 'data/' . basename($f),
                'time'     => gmdate('Y-m-d\TH:i:s\Z', $valid_ts),
                'lead_min' => $lead,
                'label'    => sprintf('%02d:%02d UTC (+%dmin)', (int)gmdate('H', $valid_ts),
                                      (int)gmdate('i', $valid_ts), $lead),
                'sort'     => $name,
            ];
        }
    }

    usort($obs, fn($a, $b) => strcmp($a['time'], $b['time']));
    foreach ($fcst as &$arr) {
        usort($arr, fn($a, $b) => strcmp($a['time'], $b['time']));
        foreach ($arr as &$f) unset($f['sort']);
    }
    foreach ($obs as &$f) unset($f['sort']);

    $bounds_file = $dir . 'bounds.json';
    $bounds = file_exists($bounds_file)
        ? json_decode(file_get_contents($bounds_file), true) : null;

    $meta_file = $dir . 'meta.json';
    $meta = file_exists($meta_file)
        ? json_decode(file_get_contents($meta_file), true) : null;

    echo json_encode(['obs' => $obs, 'fcst' => $fcst, 'bounds' => $bounds, 'meta' => $meta]);
    exit;
}

$v = '8';   // cache-busting statycznych zasobów
?>
<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Radar IMGW – Prognoza STEPS</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="css/style.css?v=<?= $v ?>"/>
</head>
<body>

<!-- ── Loading ── -->
<div id="loading"><div class="loading-inner"><div class="spinner"></div><span>Ładowanie danych…</span></div></div>

<!-- ── Mapa ── -->
<div id="map"></div>

<!-- ── Toolbar ── -->
<header id="toolbar">
  <div id="brand">
    <span class="logo">STEPS<span class="accent">·</span>radar</span>
    <span class="sub">IMGW · S-PROG nowcast</span>
  </div>

  <!-- Przełącznik typu prognozy (widoczny, gdy dostępne ≥2 typy) -->
  <div id="type-toggle" class="seg hidden"></div>

  <div id="tb-clock">
    <span id="time-clock">--:--</span>
    <span id="time-date">--.--.----</span>
    <span id="forecast-badge" class="hidden">PROGNOZA</span>
  </div>
</header>

<!-- ── Pasek kolorów ── -->
<div id="colorbar">
  <div id="colorbar-bar"><span id="colorbar-label"></span></div>
  <div id="colorbar-ticks"></div>
</div>

<!-- ── Info box (odczyt pod kursorem) ── -->
<div id="info-box">
  <div id="info-time">–</div>
  <div id="info-value">–</div>
  <div id="info-hint">najedź na mapę</div>
</div>

<!-- ── Przyciski boczne ── -->
<div id="side-actions">
  <button class="side-btn" data-panel="info" title="O produkcie">ℹ</button>
  <button class="side-btn" data-panel="settings" title="Ustawienia">⚙</button>
</div>

<!-- ── Panel boczny ── -->
<div id="side-panel">
  <div class="sp-header">
    <h4 id="sp-title">Dane punktu</h4>
    <button id="sp-close" title="Zamknij">✕</button>
  </div>

  <!-- Dane punktowe (kliknięcie na mapie) -->
  <div id="sp-point" class="sp-page">
    <div class="sp-coords" id="sp-coords">Kliknij na mapie, aby zobaczyć dane punktu.</div>
    <div class="sp-loading" id="sp-loading" style="display:none">⏳ Ładowanie…</div>
    <div class="sp-hint" id="sp-point-hint" style="display:none"></div>
    <div id="sp-charts" style="display:none">
      <div class="chart-block">
        <h5>Natężenie opadu [mm/h] — obserwacje + prognoza S-PROG</h5>
        <div class="chart-wrap h-rain"><canvas id="chart-rain"></canvas></div>
      </div>
    </div>
  </div>

  <!-- Ustawienia -->
  <div id="sp-settings" class="sp-page">
    <div class="sp-row">
      <span class="sp-label">Krycie warstwy radarowej</span>
      <div class="sp-ctrl">
        <input id="opacity-slider" type="range" min="10" max="100" value="82" class="sp-slider">
        <span class="sp-val" id="opacity-val">82%</span>
      </div>
    </div>
    <div class="sp-row">
      <span class="sp-label">Prędkość animacji</span>
      <div class="sp-ctrl">
        <span class="sp-icon">🐢</span>
        <input id="speed-slider" type="range" min="0" max="6" step="1" value="4" class="sp-slider">
        <span class="sp-icon">🐇</span>
      </div>
    </div>
    <div class="sp-row">
      <span class="sp-label">Styl mapy</span>
      <div class="sp-ctrl">
        <button class="map-style-btn" data-style="dark">Ciemna</button>
        <button class="map-style-btn" data-style="light">Jasna</button>
      </div>
    </div>
  </div>

  <!-- O produkcie -->
  <div id="sp-info" class="sp-page">
    <p class="sp-prod-name" id="sp-info-name">—</p>
    <p class="sp-prod-desc" id="sp-info-desc"></p>
  </div>
</div>

<!-- ── Odtwarzacz ── -->
<footer id="controls">
  <button id="btn-play" title="Odtwórz / pauza (Spacja)">▶</button>
  <div id="timeline">
    <input id="time-slider" type="range" min="0" max="0" value="0" class="time-slider">
    <div id="tick-row">
      <span id="tick-start">–</span>
      <span id="tick-now"></span>
      <span id="tick-end">–</span>
    </div>
  </div>
  <div id="frame-info">
    <div id="frame-label">Ładowanie…</div>
    <div id="frame-badge"></div>
  </div>
</footer>

<!-- Biblioteki zewnętrzne (klasyczne skrypty) przed modułem ES -->
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script type="module" src="js/app.js?v=<?= $v ?>"></script>
</body>
</html>
