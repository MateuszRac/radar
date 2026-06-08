// ── Odtwarzacz animacji ───────────────────────────────────────────────────────
import { SPEED_STEPS, DEFAULT_SPEED_IDX, END_PAUSE_MS } from './config.js';

export function createPlayer({ onFrame, onClear }) {
  let frames        = [];
  let idx           = 0;
  let forecastStart = -1;     // indeks pierwszej klatki prognozy (-1 = brak)
  let timer         = null;
  let endTimer      = null;
  let playing       = false;
  let playMs        = SPEED_STEPS[DEFAULT_SPEED_IDX];

  const slider  = document.getElementById('time-slider');
  const btnPlay = document.getElementById('btn-play');

  // Gradient suwaka: niebieski (obserwacje) → pomarańczowy (prognoza),
  // jasna część = postęp do bieżącej klatki.
  function setFill() {
    const total = frames.length;
    if (!slider) return;
    if (total <= 1) { slider.style.background = 'var(--border)'; return; }

    const pct  = (idx / (total - 1)) * 100;
    const hasFc = forecastStart > 0 && forecastStart < total;
    const fPct = hasFc ? (forecastStart / (total - 1)) * 100 : 100;
    const OBS = '#1d6fe0', FC = '#e07020', DIM = 'rgba(224,112,32,.22)', TRK = 'var(--border)';

    if (!hasFc) {
      slider.style.background = `linear-gradient(to right, ${OBS} ${pct}%, ${TRK} ${pct}%)`;
    } else if (idx < forecastStart) {
      slider.style.background =
        `linear-gradient(to right, ${OBS} ${pct}%, ${TRK} ${pct}%, ${TRK} ${fPct}%, ${DIM} ${fPct}%)`;
    } else {
      slider.style.background =
        `linear-gradient(to right, ${OBS} ${fPct}%, ${FC} ${fPct}%, ${FC} ${pct}%, ${DIM} ${pct}%)`;
    }
  }

  function render() {
    if (!frames.length) return;
    if (slider) slider.value = idx;
    setFill();
    onFrame?.(frames[idx], idx, frames.length, forecastStart);
  }

  function clearTimers() {
    if (timer)    { clearInterval(timer); timer = null; }
    if (endTimer) { clearTimeout(endTimer); endTimer = null; }
  }

  function stop() {
    clearTimers();
    playing = false;
    if (btnPlay) { btnPlay.textContent = '▶'; btnPlay.classList.remove('playing'); }
  }

  function play() {
    if (!frames.length) return;
    playing = true;
    if (btnPlay) { btnPlay.textContent = '⏸'; btnPlay.classList.add('playing'); }
    clearTimers();
    timer = setInterval(() => {
      if (idx >= frames.length - 1) {
        // koniec pętli — pauza i restart od początku
        clearInterval(timer); timer = null;
        endTimer = setTimeout(() => { idx = 0; render(); if (playing) play(); }, END_PAUSE_MS);
        return;
      }
      idx++;
      render();
    }, playMs);
  }

  function toggle() { playing ? stop() : play(); }

  function setSpeed(ms) {
    playMs = ms;
    if (playing) { play(); }
  }

  function goto(i) {
    stop();
    idx = Math.max(0, Math.min(i, frames.length - 1));
    render();
  }

  function loadFrames(newFrames) {
    const was = playing;
    stop();
    frames = newFrames ?? [];
    forecastStart = frames.findIndex(f => f.is_forecast);
    // Stajemy na ostatniej obserwacji (nie na prognozie)
    idx = forecastStart > 0 ? forecastStart - 1 : frames.length - 1;
    if (slider) { slider.min = 0; slider.max = Math.max(0, frames.length - 1); }
    if (frames.length) { render(); if (was) play(); }
    else { setFill(); onClear?.(); }
  }

  // Aktualizacja przy auto-odświeżeniu — zachowuje pozycję / przeskakuje na nowy skan
  function updateFrames(newFrames) {
    const updated = newFrames ?? [];
    if (!updated.length) return;
    const curTime = frames[idx]?.time;
    const oldFc = frames.findIndex(f => f.is_forecast);
    const oldLastReal = oldFc > 0 ? oldFc - 1 : frames.length - 1;
    const wasAtLastReal = idx === oldLastReal;

    frames = updated;
    forecastStart = frames.findIndex(f => f.is_forecast);
    if (slider) slider.max = Math.max(0, frames.length - 1);

    const newLastReal = forecastStart > 0 ? forecastStart - 1 : frames.length - 1;
    if (wasAtLastReal) {
      idx = newLastReal;
    } else {
      const restored = curTime ? frames.findIndex(f => f.time === curTime) : -1;
      idx = restored >= 0 ? restored : newLastReal;
    }
    if (!playing) render(); else setFill();
  }

  btnPlay?.addEventListener('click', toggle);
  slider?.addEventListener('input', () => { stop(); idx = +slider.value; setFill(); render(); });

  document.addEventListener('keydown', e => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    if (e.key === ' ')          { e.preventDefault(); toggle(); }
    if (e.key === 'ArrowRight') { goto(idx + 1); }
    if (e.key === 'ArrowLeft')  { goto(idx - 1); }
  });

  return {
    loadFrames, updateFrames, setSpeed, stop, play, toggle, goto,
    get currentFrame() { return frames[idx] ?? null; },
    get count()        { return frames.length; },
  };
}
