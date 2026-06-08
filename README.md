# STEPS — nowcasting opadów z radaru IMGW

Biblioteka i aplikacja webowa do pobierania, dekodowania i wizualizacji danych
radarowych (HDF5/ODIM) z publicznego API **IMGW**, połączona z krótkoterminową
prognozą opadu (**nowcasting**) i interaktywną mapą **Leaflet**.

Domyślnie liczone są dwie deterministyczne metody prognozy — **S-PROG** i **LINDA**
(pysteps) — każda opcjonalnie zmiksowana z modelem numerycznym **ICON-EU** (DWD).
Aplikacja potrafi też wysyłać **ostrzeżenia Telegram** o silnym opadzie w
wybranych punktach.

> Źródłem danych radarowych jest Instytut Meteorologii i Gospodarki Wodnej –
> Państwowy Instytut Badawczy (dane przetworzone). Dane NWP: DWD OpenData (ICON-EU).

---

## Funkcje

- ⛈️ **Nowcasting opadu** kompozytu `DPSRI` (natężenie w mm/h) z krokiem 5 min do +120 min.
- 🧠 **Dwie metody deterministyczne**: S-PROG (kaskada widmowa) i LINDA (model cech + autoregresja).
- 🌍 **Blend z ICON-EU**: radar dominuje na krótkich horyzontach, model numeryczny na dłuższych.
- 🗺️ **Web viewer**: pełnoekranowa mapa, animacja, przełącznik typów prognozy, odczyt wartości pod kursorem, meteogram dla klikniętego punktu.
- 📡 **Cache radarowy**: trzyma N ostatnich plików HDF5, pobiera tylko brakujące.
- 🚀 **Atomowy transfer FTP**: aktualizacja bez „okna bez danych” (manifest + uprzednie wgranie nowych plików).
- 🔔 **Alerty Telegram**: powiadomienie, gdy prognozowane natężenie w punkcie przekroczy próg (domyślnie 10 mm/h), z informacją z jakiego algorytmu pochodzi.

---

## Wymagania

- **Python 3.12** (zarządzane przez [uv](https://docs.astral.sh/uv/); pinowane w `.python-version`)
- **eccodes** (biblioteka systemowa GRIB2) — wymagana przez ICON-EU
- Do hostingu webowego: **PHP 8.0+** z rozszerzeniem **GD** (dla `point_data.php`)

## Instalacja

```bash
uv sync   # tworzy .venv, pobiera Pythona i instaluje wszystkie zależności
```

`uv sync` instaluje pakiety projektu `imgw` i `radar` jako *editable* (z `src/`).
Katalog `src/pysteps/` celowo **nie** jest instalowany jako pakiet (kolidowałby z
biblioteką `pysteps`) — skrypty ładowane są po ścieżce pliku.

## Konfiguracja

Utwórz plik `.env` w katalogu głównym:

```dotenv
# Transfer FTP/FTPS (wysyłka overlayów na serwer)
FTP_HOST=twoj.serwer.pl
FTP_PORT=21
FTP_USER=login
FTP_PASSWORD=haslo
FTP_TLS=false
FTP_REMOTE_IMG_DIR=/public_html/steps/data

# Ostrzeżenia Telegram (opcjonalne)
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

Monitorowane punkty dla alertów definiujesz w `config/watch_points.json`:

```json
[
  {"name": "Warszawa", "lat": 52.227325, "lon": 20.989656},
  {"name": "Żulin",    "lat": 51.076906, "lon": 23.180402}
]
```

> Bez `FTP_*` transfer jest pomijany; bez `TELEGRAM_*` alerty są pomijane —
> generowanie obrazów działa niezależnie.

---

## Użycie

### Pełny pipeline (zalecane)

`src/pipeline/pipeline.py` wykonuje: **czyszczenie `www/data` → generowanie → atomowy transfer FTP**.

```bash
uv run python src/pipeline/pipeline.py                # S-PROG + LINDA (bez ICON)
uv run python src/pipeline/pipeline.py --icon         # + warianty z ICON-EU (do 4 typów)
uv run python src/pipeline/pipeline.py --no-linda     # tylko S-PROG
uv run python src/pipeline/pipeline.py --no-upload    # bez transferu FTP (tylko lokalnie)
uv run python src/pipeline/pipeline.py --remote-dir /sciezka/na/serwerze
```

### Pojedyncze generatory

```bash
uv run python src/pysteps/steps_nowcast_imgw.py              # S-PROG + LINDA
uv run python src/pysteps/steps_nowcast_imgw.py --no-linda   # tylko S-PROG
uv run python src/pysteps/steps_nowcast_imgw.py --ensemble   # + ensemble STEPS (mean + prawdopodobieństwa)
uv run python src/pysteps/steps_nowcast_imgw_icon.py         # + warianty z ICON-EU
```

Typowe wdrożenie produkcyjne to uruchamianie `pipeline.py` z crona co ~5 minut.

---

## Typy prognozy

Każdy „typ” to deterministyczny produkt opadu (mm/h), renderowany tą samą paletą RATE.
Web viewer pokazuje przełącznik, gdy dostępne są ≥2 typy.

| Klucz       | Metoda                | Generowane przez            |
|-------------|-----------------------|-----------------------------|
| `det`       | S-PROG                | zawsze                      |
| `linda`     | LINDA                 | domyślnie (chyba że `--no-linda`) |
| `icon`      | S-PROG + ICON-EU      | `--icon`                    |
| `lindaicon` | LINDA + ICON-EU       | `--icon` (gdy LINDA włączona)|

Opcjonalnie (`--ensemble`) dochodzi stochastyczny ensemble STEPS: `mean` oraz
prawdopodobieństwa `prob01` (P>0.1 mm/h) i `prob10` (P>10 mm/h).

---

## Jak to działa

```
API IMGW ──► cache HDF5 (data/imgw/polrad, N ostatnich, pobiera tylko brakujące)
         ──► dekodowanie ODIM → mm/h ──► pole ruchu (Lucas-Kanade)
         ──► S-PROG / LINDA [+ blend ICON-EU] ──► overlaye PNG (EPSG:3857, RATE)
         ──► www/data/ + manifest.json ──► atomowy transfer FTP ──► serwer WWW
         ──► alerty Telegram (gdy opad > progu w monitorowanym punkcie)
```

- **Cache radarowy** (`data/imgw/polrad`) — między uruchomieniami pobiera się zwykle 1 nowy plik; starsze są przycinane do N najnowszych.
- **Atomowy transfer** — najpierw wgrywane są nowe pliki, potem mały `manifest.json` (przełączenie zestawu danych), na końcu kasowane są stare. Viewer czyta `manifest.json`, więc nigdy nie trafi na niespójny/niekompletny stan.
- **Alerty Telegram** — sprawdzane są czyste prognozy radarowe (S-PROG/LINDA); cooldown per (punkt, algorytm) w `data/telegram_alert_state.json`.

---

## Web viewer (`www/`)

Statyczne moduły ES + lekki backend PHP. Serwuj katalog `www/` przez PHP:

```bash
php -S localhost:8000 -t www   # podgląd lokalny (wymaga PHP + GD)
```

- `index.php` — strona + API `?api=1` (czyta `manifest.json`, fallback: skan katalogu).
- `point_data.php` — szereg czasowy dla klikniętego punktu (odczyt pikseli przez GD).
- `js/` — `config`, `api`, `map`, `player`, `charts`, `app` (animacja, przełącznik typów, meteogram, hover).
- `css/style.css` — pełnoekranowa mapa, szklany toolbar/stopka, pasek kolorów, panel boczny.

---

## Struktura projektu

```
src/
  imgw/        # klient API IMGW (lista + pobieranie plików)
  radar/       # dekoder HDF5/ODIM, renderer, palety (RATE = źródło prawdy)
  nwp/         # klient DWD OpenData (ICON-EU GRIB2)
  pysteps/     # skrypty nowcastingu (steps_nowcast_imgw[.py / _icon.py])
  transfer/    # FtpUploader (sync_atomic – transfer bez przestoju)
  telegram/    # alerty o silnym opadzie
  pipeline/    # pipeline.py – orkiestracja całości
www/           # viewer (PHP + JS modules + CSS)
config/        # watch_points.json
data/          # cache radarowy + stan alertów (git-ignored)
```

---

## Uwagi techniczne

- **Paleta RATE** (23 kolory, log-skala 0.01–63 mm/h) jest źródłem prawdy w `src/radar/palette.py` i musi być **identyczna** w `www/js/config.js` i `www/point_data.php` (front mapuje kolor piksela → wartość). Przy zmianie palety podbij `$v` w `index.php`.
- Dokumentacja i logi w kodzie są po polsku — utrzymuj tę konwencję.
- LINDA działa na natężeniu w jednostkach **liniowych** (mm/h), nie w dB; używa detektora cech `shitomasi` (OpenCV), by nie wymagać scikit-image.

## Źródła danych

- **Radar**: IMGW-PIB (kompozyt DPSRI, `danepubliczne.imgw.pl`).
- **NWP**: DWD OpenData — ICON-EU (`opendata.dwd.de`).
- **Podkład mapy**: OpenStreetMap / CARTO.
