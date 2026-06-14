<?php
/**
 * point_data.php — zwraca dane czasowe dla klikniętego punktu (lat, lng).
 *
 * Odczytuje kolor piksela z każdego PNG w www/data/ i mapuje go wstecz
 * na wartość fizyczną przez dopasowanie najbliższego koloru z palety.
 * Wymaga PHP GD (imagecreatefrompng).
 *
 * GET: lat, lng
 * Zwraca JSON: { lat, lng, obs:[{time,label,value}], fcst:[{time,label,lead_min,det,icon}] }
 * Wartości to natężenie opadu [mm/h]: obserwacje + prognoza S-PROG (det)
 * oraz — jeśli dostępna — prognoza S-PROG + ICON-EU (icon).
 */
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

// ── Walidacja ─────────────────────────────────────────────────────────────────
$lat = isset($_GET['lat']) ? (float)$_GET['lat'] : null;
$lng = isset($_GET['lng']) ? (float)$_GET['lng'] : null;

if ($lat === null || $lng === null) {
    exit(json_encode(['error' => 'Brak parametrów lat/lng']));
}
if (!function_exists('imagecreatefrompng')) {
    exit(json_encode(['error' => 'PHP GD library jest wymagana (imagecreatefrompng)']));
}

$dir         = __DIR__ . '/data/';
$bounds_path = $dir . 'bounds.json';

if (!file_exists($bounds_path)) {
    exit(json_encode(['error' => 'Brak bounds.json — uruchom najpierw skrypt Python']));
}

$bounds = json_decode(file_get_contents($bounds_path), true);
[$lat_sw, $lng_sw] = $bounds['sw'];
[$lat_ne, $lng_ne] = $bounds['ne'];

if ($lat < $lat_sw || $lat > $lat_ne || $lng < $lng_sw || $lng > $lng_ne) {
    exit(json_encode(['error' => 'Punkt poza zasięgiem radaru', 'out_of_bounds' => true]));
}

// ── Projekcja EPSG:3857 ───────────────────────────────────────────────────────
function merc(float $lat, float $lng): array {
    $R = 6378137.0;
    return [
        $lng * M_PI / 180.0 * $R,
        log(tan(M_PI / 4.0 + $lat * M_PI / 360.0)) * $R,
    ];
}

[$sw_x, $sw_y] = merc($lat_sw, $lng_sw);
[$ne_x, $ne_y] = merc($lat_ne, $lng_ne);
[$cx,   $cy  ] = merc($lat, $lng);

// Ułamkowa pozycja piksela w obrazie (0..1), identyczna logika co JS
$px_frac = ($cx - $sw_x) / ($ne_x - $sw_x);
$py_frac = 1.0 - ($cy - $sw_y) / ($ne_y - $sw_y);

// ── Odczyt piksela z PNG ──────────────────────────────────────────────────────
function readPixel(string $path, float $fx, float $fy): array {
    $img = @imagecreatefrompng($path);
    if (!$img) return [0, 0, 0, 127];

    // PNG z kanałem alfa — zapewnij tryb truecolor+alpha
    imagesavealpha($img, true);

    $w  = imagesx($img);
    $h  = imagesy($img);
    $px = max(0, min($w - 1, (int)round($fx * ($w - 1))));
    $py = max(0, min($h - 1, (int)round($fy * ($h - 1))));

    $c = imagecolorat($img, $px, $py);
    imagedestroy($img);

    // Format GD truecolor+alpha: 0xAARRGGBB (AA: 0=opaque,127=transparent)
    return [
        ($c >> 16) & 0xFF,   // R
        ($c >> 8)  & 0xFF,   // G
         $c        & 0xFF,   // B
        ($c >> 24) & 0x7F,   // A (0=opaque)
    ];
}

// ── Palety [r, g, b, wartość_reprezentatywna] ─────────────────────────────────
// Muszą być identyczne z definicjami w steps_nowcast_imgw.py i index.php

// RATE — paleta natężenia opadu z radar/palette.py (23 kolory).
// [R, G, B, wartość reprezentatywna mm/h = średnia geom. granic kubełka]
$RAIN = [
    [212,240,255,   0.0121], [160,216,240,   0.0177], [112,192,232,   0.0259],
    [ 64,168,224,   0.0379], [ 30,144,216,   0.0554], [  0,200,160,   0.0810],
    [ 64,224, 96,   0.1186], [160,240,  0,   0.1734], [255,255,  0,   0.2537],
    [255,208,  0,   0.3712], [255,153,  0,   0.5430], [255,102,  0,   0.7943],
    [255, 51,  0,   1.1620], [224,  0,  0,   1.6999], [176,  0,  0,   2.4869],
    [128,  0,  0,   3.6381], [192,  0,192,   5.3221], [153,  0,204,   7.7858],
    [102,  0,187,  11.3899], [212,176,240,  16.6625], [232,208,248,  24.3757],
    [200,200,200,  35.6594], [144,144,144,  52.1665],
];

function nearest(int $r, int $g, int $b, int $a, array $pal): ?float {
    if ($a > 63) return null;  // przezroczysty = brak danych
    $minD = PHP_INT_MAX;
    $best = null;
    foreach ($pal as $e) {
        $d = ($r-$e[0])**2 + ($g-$e[1])**2 + ($b-$e[2])**2;
        if ($d < $minD) { $minD = $d; $best = (float)$e[3]; }
    }
    return ($minD < 15000) ? $best : null;
}

// ── Przetwarzanie plików PNG ──────────────────────────────────────────────────
$obs  = [];
$fcst = [];  // klucz = lead_min

$manifest_file = $dir . 'manifest.json';
$man = is_file($manifest_file) ? json_decode(file_get_contents($manifest_file), true) : null;

// Deterministyczne produkty opadu [mm/h] pokazywane w meteogramie.
$RAIN_PRODUCTS = ['det', 'linda', 'anvil', 'icon', 'lindaicon', 'anvilicon'];

if (is_array($man) && isset($man['obs'])) {
    // ── Ścieżka główna: lista plików z manifestu (spójna w trakcie sync) ─────
    foreach (($man['obs'] ?? []) as $e) {
        $path = $dir . $e['file'];
        if (!is_file($path)) continue;
        [$r,$g,$b,$a] = readPixel($path, $px_frac, $py_frac);
        $obs[] = [
            'time'  => $e['time'],
            'label' => str_replace(' UTC', '', $e['label']),
            'value' => nearest($r, $g, $b, $a, $RAIN),
        ];
    }
    foreach ($RAIN_PRODUCTS as $prod) {
        foreach (($man['fcst'][$prod] ?? []) as $e) {
            $path = $dir . $e['file'];
            if (!is_file($path)) continue;
            [$r,$g,$b,$a] = readPixel($path, $px_frac, $py_frac);
            $lead = (int)$e['lead_min'];
            if (!isset($fcst[$lead])) {
                $fcst[$lead] = ['time' => $e['time'], 'label' => "+{$lead}min", 'lead_min' => $lead];
                foreach ($RAIN_PRODUCTS as $p) $fcst[$lead][$p] = null;
            }
            $fcst[$lead][$prod] = nearest($r, $g, $b, $a, $RAIN);
        }
    }
} else {
    // ── Fallback: skanowanie katalogu (gdy brak manifestu) ───────────────────
    foreach (glob($dir . '*.png') as $f) {
        $name       = basename($f, '.png');
        [$r,$g,$b,$a] = readPixel($f, $px_frac, $py_frac);

        if (preg_match('/^obs_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})$/', $name, $m)) {
            $obs[] = [
                'time'  => "{$m[1]}-{$m[2]}-{$m[3]}T{$m[4]}:{$m[5]}:00Z",
                'label' => "{$m[4]}:{$m[5]}",
                'value' => nearest($r, $g, $b, $a, $RAIN),
            ];
            continue;
        }

        if (!preg_match(
            '/^fcst_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})_plus_(\d+)_(det|linda|anvil|icon|lindaicon|anvilicon)$/',
            $name, $m
        )) continue;

        $lead  = (int)$m[6];
        $prod  = $m[7];
        $valid = gmmktime((int)$m[4], (int)$m[5], 0, (int)$m[2], (int)$m[3], (int)$m[1]) + $lead * 60;

        if (!isset($fcst[$lead])) {
            $fcst[$lead] = ['time' => gmdate('Y-m-d\TH:i:s\Z', $valid), 'label' => "+{$lead}min", 'lead_min' => $lead];
            foreach ($RAIN_PRODUCTS as $p) $fcst[$lead][$p] = null;
        }
        $fcst[$lead][$prod] = nearest($r, $g, $b, $a, $RAIN);
    }
}

// ── Sortowanie i odpowiedź ────────────────────────────────────────────────────
usort($obs, fn($a, $b) => strcmp($a['time'], $b['time']));
ksort($fcst);

echo json_encode([
    'lat'  => round($lat, 5),
    'lng'  => round($lng, 5),
    'obs'  => array_values($obs),
    'fcst' => array_values($fcst),
]);
