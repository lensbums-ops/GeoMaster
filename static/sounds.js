/* GeoMaster — synthesised sound effects via Web Audio API.
   No external files. All sounds are procedurally generated. */

const Sounds = (() => {
  let _ctx = null;
  let _muted = localStorage.getItem('gm_muted') === '1';

  function ctx() {
    if (!_ctx) _ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (_ctx.state === 'suspended') _ctx.resume();
    return _ctx;
  }

  // Primitive: play a single tone
  function tone(freq, type, duration, gain, delay = 0) {
    if (_muted) return;
    const c = ctx();
    const osc = c.createOscillator();
    const g   = c.createGain();
    osc.connect(g);
    g.connect(c.destination);
    osc.type = type;
    osc.frequency.value = freq;
    const t0 = c.currentTime + delay;
    g.gain.setValueAtTime(gain, t0);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
    osc.start(t0);
    osc.stop(t0 + duration + 0.01);
  }

  // Short filtered noise burst (card pop / reveal)
  function noiseBurst(duration, gain, delay = 0) {
    if (_muted) return;
    const c   = ctx();
    const len = Math.ceil(c.sampleRate * duration);
    const buf = c.createBuffer(1, len, c.sampleRate);
    const d   = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    const src = c.createBufferSource();
    src.buffer = buf;
    const flt = c.createBiquadFilter();
    flt.type = 'bandpass';
    flt.frequency.value = 1200;
    flt.Q.value = 0.8;
    const g = c.createGain();
    src.connect(flt); flt.connect(g); g.connect(c.destination);
    const t0 = c.currentTime + delay;
    g.gain.setValueAtTime(gain, t0);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
    src.start(t0);
    src.stop(t0 + duration + 0.01);
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  function guess() {
    tone(680, 'sine',   0.12, 0.28);
    tone(900, 'sine',   0.07, 0.12, 0.06);
  }

  function perfect() {
    // Bright ascending chord C5-E5-G5-C6
    [523, 659, 784, 1047].forEach((f, i) =>
      tone(f, 'sine', 0.55, 0.38, i * 0.09));
  }

  function tick() {
    tone(880, 'square', 0.045, 0.18);
  }

  function timeout() {
    tone(320, 'sawtooth', 0.35, 0.30);
    tone(220, 'sawtooth', 0.30, 0.20, 0.22);
    tone(160, 'sawtooth', 0.25, 0.15, 0.42);
  }

  function streak() {
    [380, 520, 720, 960].forEach((f, i) =>
      tone(f, 'sine', 0.18, 0.28, i * 0.07));
  }

  function reveal() {
    noiseBurst(0.10, 0.22);
    tone(520, 'sine', 0.14, 0.18, 0.02);
  }

  function fanfare() {
    // C5-E5-G5-E5-C6 triumphant
    const melody = [[523,0],[659,0.13],[784,0.26],[659,0.39],[1047,0.52]];
    melody.forEach(([f, d]) => tone(f, 'sine', 0.42, 0.40, d));
  }

  function toggleMute() {
    _muted = !_muted;
    localStorage.setItem('gm_muted', _muted ? '1' : '0');
    return _muted;
  }

  function isMuted() { return _muted; }

  return { guess, perfect, tick, timeout, streak, reveal, fanfare, toggleMute, isMuted };
})();
