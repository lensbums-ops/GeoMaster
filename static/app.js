/* GeoMaster Flask — online multiplayer frontend
   Backend game logic remains in Python; this file handles:
   - room create/join lobby flow
   - polling shared room/game state from Flask
   - Leaflet map display
   - rendering synced game state for each player browser
   - client-side countdown display based on server deadlines
   - reveal animations and local polish
*/

// ─── Constants ───────────────────────────────────────────────────────────────
const POLL_INTERVAL_MS = 1500;
const MAX_DISTANCE_KM = 10000;
const CATEGORY_DEFS = [
  { mode: 'country_flag',     icon: '🚩', name: 'Country Flag',    desc: 'Identify the country from its flag' },
  { mode: 'country_capital',  icon: '🏙️', name: 'Capital City',    desc: 'Which country has this capital?' },
  { mode: 'country_stats',    icon: '📊', name: 'Country Facts',   desc: '3 statistical clues about a country' },
  { mode: 'country_landmark', icon: '🏛️', name: 'Landmark',        desc: 'A famous landmark description' },
  { mode: 'city_name',        icon: '📍', name: 'Find the City',   desc: 'Locate a named city on the map' },
  { mode: 'city_facts',       icon: '🧠', name: 'City Facts',      desc: '3 clues about a mystery city' },
  { mode: 'detective_city',   icon: '🕵️', name: 'Detective City',  desc: 'Unlock up to 3 clues — fewer clues means a bigger score multiplier' },
];

// ─── State ───────────────────────────────────────────────────────────────────
let currentRoom = null;
let currentState = null;
let syncInterval = null;

let gameMap = null;
let resultsMap = null;
let guessMarker = null;
let pendingGuess = null;
let revealLayers = [];
let peekLayer = null;

let timerInterval = null;
let timerDeadlineMs = null;
let timerCanAutoTimeout = false;
let timerTriggeredDeadline = null;

let activeResultsKey = null;
let activeFinalKey = null;
let lastPerfectEventKey = null;
let lastStreakEventKey = null;

// ─── API helper ──────────────────────────────────────────────────────────────
async function api(url, method = 'GET', body = null, { showErrors = true } = {}) {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : null,
  }).catch(() => null);

  if (!res) {
    if (showErrors) showToast('Could not reach the server.');
    return null;
  }

  const payload = await res.json().catch(() => ({ error: 'Unknown server response' }));
  if (!res.ok) {
    if (showErrors) showToast(payload.error || 'Request failed.');
    return null;
  }
  return payload;
}

// ─── Boot / polling ──────────────────────────────────────────────────────────
async function refreshFromServer() {
  const payload = await api('/api/bootstrap', 'GET', null, { showErrors: false });
  if (payload) applyPayload(payload);
}

function startSync() {
  if (syncInterval) return;
  syncInterval = setInterval(refreshFromServer, POLL_INTERVAL_MS);
}

function stopSync() {
  if (syncInterval) {
    clearInterval(syncInterval);
    syncInterval = null;
  }
}

function applyPayload(payload) {
  if (!payload || !payload.room) {
    currentRoom = null;
    currentState = null;
    activeResultsKey = null;
    activeFinalKey = null;
    stopSync();
    stopTimer();
    clearRevealLayers();
    showScreen('screen-start');
    return;
  }

  currentRoom = payload.room;
  startSync();

  // Keep rematch button in sync while on final screen
  if (document.getElementById('screen-final').classList.contains('active')) {
    updateRematchButton();
  }

  if (!payload.room.started || !payload.game) {
    currentState = null;
    activeResultsKey = null;
    activeFinalKey = null;
    stopTimer();
    showLobby(payload.room);
    return;
  }

  processState(payload.game);
}

// ─── Maps ────────────────────────────────────────────────────────────────────
function initGameMap() {
  if (gameMap) return;
  gameMap = L.map('game-map', { center: [20, 0], zoom: 2, minZoom: 2, maxZoom: 6, worldCopyJump: false });
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd', noWrap: true,
  }).addTo(gameMap);
  gameMap.on('click', onMapClick);
}

function initResultsMap() {
  if (resultsMap) { resultsMap.remove(); resultsMap = null; }
  resultsMap = L.map('results-map', { center: [20, 0], zoom: 2 });
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd', noWrap: true,
  }).addTo(resultsMap);
}

function invalidateMapsSoon() {
  requestAnimationFrame(() => {
    if (gameMap && document.getElementById('screen-game').classList.contains('active')) {
      gameMap.invalidateSize();
      setTimeout(() => {
        if (gameMap) gameMap.invalidateSize();
      }, 120);
    }
    if (resultsMap && document.getElementById('screen-results').classList.contains('active')) {
      resultsMap.invalidateSize();
      setTimeout(() => {
        if (resultsMap) resultsMap.invalidateSize();
      }, 120);
    }
  });
}

function onMapClick(e) {
  if (!currentState || currentState.phase !== 'guessing' || !currentState.can_guess) return;

  const lat = e.latlng.lat;
  const lng = e.latlng.lng;
  if (lat < -85 || lat > 85) {
    showToast('Click on the main map area.');
    return;
  }

  pendingGuess = { lat, lng };
  if (guessMarker) gameMap.removeLayer(guessMarker);

  const p = currentState.players[currentState.viewer_index] ?? currentState.players[0];
  guessMarker = L.marker([lat, lng], {
    icon: L.divIcon({
      className: '',
      html: `<div style="width:20px;height:20px;border-radius:50%;background:${p.color};border:3px solid white;box-shadow:0 2px 10px rgba(0,0,0,.5)"></div>`,
      iconSize: [20, 20], iconAnchor: [10, 10],
    }),
  }).addTo(gameMap);

  const confirmBtn = document.getElementById('confirm-btn');
  confirmBtn.disabled = false;
  confirmBtn.textContent = 'Confirm Guess';
  document.getElementById('map-hint').textContent = 'Marker placed — use jokers or confirm.';
}

function clearGuessMarker() {
  pendingGuess = null;
  if (guessMarker && gameMap) {
    gameMap.removeLayer(guessMarker);
    guessMarker = null;
  }
}

// ─── Lobby ───────────────────────────────────────────────────────────────────
function showLobby(room) {
  showScreen('screen-lobby');

  const inviteUrl = `${window.location.origin}${window.location.pathname}?room=${room.room_code}`;
  document.getElementById('lobby-room-code').textContent = room.room_code;
  document.getElementById('invite-link').textContent = inviteUrl;
  document.getElementById('lobby-status').textContent =
    room.is_host
      ? `You are hosting as ${room.viewer_name}. Start when everyone has joined.`
      : `Joined as ${room.viewer_name}. Waiting for ${room.host_name} to start the match.`;

  document.getElementById('lobby-player-list').innerHTML = room.players.map((player, index) => {
    const isViewer = room.viewer_index === index ? 'You' : '';
    const isHost = room.host_name === player.name ? 'Host' : '';
    const tags = [isHost, isViewer].filter(Boolean).map(tag =>
      `<span class="lobby-player-tag">${tag}</span>`
    ).join('');
    return `<div class="lobby-player">
      <div class="mini-dot" style="background:${player.color}"></div>
      <div class="lobby-player-name">${player.name}</div>
      ${tags}
    </div>`;
  }).join('');

  document.querySelectorAll('#lobby-round-options .round-option').forEach(btn => {
    btn.classList.toggle('active', Number(btn.dataset.rounds) === room.rounds);
    btn.disabled = !room.is_host;
  });

  const startBtn = document.getElementById('lobby-start-btn');
  startBtn.disabled = !room.is_host;
  startBtn.textContent = room.is_host ? 'Start Match' : 'Waiting for Host';
  document.getElementById('lobby-round-note').textContent =
    room.is_host
      ? 'Choose the round count, then start the room.'
      : `${room.host_name} controls the rounds and starts the match.`;
}

// ─── Timer ───────────────────────────────────────────────────────────────────
function startTimer(deadlineMs, canAutoTimeout) {
  if (!deadlineMs) {
    stopTimer();
    document.getElementById('timer-value').textContent = '—';
    return;
  }

  if (timerInterval && timerDeadlineMs === deadlineMs && timerCanAutoTimeout === canAutoTimeout) {
    renderTimer();
    return;
  }

  stopTimer(false);
  timerDeadlineMs = deadlineMs;
  timerCanAutoTimeout = canAutoTimeout;
  timerTriggeredDeadline = null;
  renderTimer();

  timerInterval = setInterval(() => {
    renderTimer();
    if (timerCanAutoTimeout && timerDeadlineMs && Date.now() >= timerDeadlineMs && timerTriggeredDeadline !== timerDeadlineMs) {
      timerTriggeredDeadline = timerDeadlineMs;
      handleTimeout();
    }
  }, 250);
}

function stopTimer(reset = true) {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
  if (reset) {
    timerDeadlineMs = null;
    timerCanAutoTimeout = false;
    timerTriggeredDeadline = null;
  }
}

let _lastTickSecond = -1;

function renderTimer() {
  const el = document.getElementById('timer-value');
  if (!timerDeadlineMs) {
    el.textContent = '—';
    el.className = 'timer-value';
    _lastTickSecond = -1;
    return;
  }

  const leftMs = Math.max(0, timerDeadlineMs - Date.now());
  const leftSeconds = Math.ceil(leftMs / 1000);
  el.textContent = `${leftSeconds}s`;
  el.className = leftSeconds <= 10 ? 'timer-value warn' : 'timer-value';

  // Tick sound in last 5 seconds, once per second, only for active guesser
  if (timerCanAutoTimeout && leftSeconds <= 5 && leftSeconds > 0 && leftSeconds !== _lastTickSecond) {
    _lastTickSecond = leftSeconds;
    Sounds.tick();
  }
}

async function handleTimeout() {
  if (!currentState || !currentState.can_guess) return;
  if (pendingGuess) {
    // Player placed a marker but didn't confirm — submit it automatically
    const confirmBtn = document.getElementById('confirm-btn');
    confirmBtn.disabled = true;
    if (peekLayer && gameMap) { gameMap.removeLayer(peekLayer); peekLayer = null; }
    document.getElementById('peek-overlay').style.display = 'none';
    Sounds.guess();
    const payload = await api('/api/guess', 'POST', pendingGuess, { showErrors: false });
    if (payload) applyPayload(payload);
  } else {
    Sounds.timeout();
    const payload = await api('/api/timeout', 'POST', {}, { showErrors: false });
    if (payload) applyPayload(payload);
  }
}

// ─── Render helpers ──────────────────────────────────────────────────────────
function renderClue(question) {
  const box = document.getElementById('clue-body');
  const lbl = document.getElementById('clue-label');
  const detectiveControls = document.getElementById('detective-controls');
  const detectiveMeta = document.getElementById('detective-meta');
  const detectiveBtn = document.getElementById('detective-hint-btn');

  if (!question) {
    box.innerHTML = '';
    detectiveControls.classList.remove('show');
    detectiveMeta.textContent = '';
    detectiveBtn.disabled = true;
    return;
  }

  const modeLabels = {
    country_flag: 'Which country does this flag belong to?',
    country_capital: 'Which country has this capital city?',
    country_stats: 'Which country matches these clues?',
    country_landmark: 'Which country is described by this landmark clue?',
    city_name: 'Where is this city located on the map?',
    city_facts: 'Which city matches these facts?',
    detective_city: 'Detective City — solve it with as few clues as possible',
  };
  lbl.textContent = modeLabels[question.mode] || '';

  const { mode, item } = question;
  let html = '';
  if (mode === 'country_flag') {
    html = `<div class="flag-big">${flagEmoji(item.iso)}</div>`;
  } else if (mode === 'country_capital') {
    html = `<div class="cap-box"><div class="cap-name">${item.capital}</div><div class="cap-sub">Capital city clue</div></div>`;
  } else if (mode === 'country_stats') {
    html = `<div class="stats-list">${item.clues.map((c, i) =>
      `<div class="stat-row"><div class="stat-n">${i + 1}</div><div>${c}</div></div>`).join('')}</div>`;
  } else if (mode === 'country_landmark') {
    html = `<div class="landmark-box">${item.landmark}</div>`;
  } else if (mode === 'city_name') {
    html = `<div class="cap-box"><div class="cap-name">${item.name}</div><div class="cap-sub">Find this city on the map</div></div>`;
  } else if (mode === 'city_facts') {
    html = `<div class="stats-list">${item.facts.map((f, i) =>
      `<div class="stat-row"><div class="stat-n">${i + 1}</div><div>${f}</div></div>`).join('')}</div>`;
  } else if (mode === 'detective_city') {
    html = `<div class="stats-list">${item.detective_clues.map((f, i) =>
      `<div class="stat-row"><div class="stat-n">${i + 1}</div><div>${f}</div></div>`).join('')}</div>`;
  }
  box.innerHTML = html;

  if (mode === 'detective_city') {
    const revealed = currentState?.detective_progress?.revealed_clues ?? item.detective_clues.length;
    const multiplier = currentState?.detective_progress?.score_multiplier ?? 3;
    detectiveMeta.textContent = `Clues used: ${revealed}/3 · Current score multiplier: ×${multiplier}`;
    detectiveBtn.disabled = !currentState?.can_guess || revealed >= 3;
    detectiveBtn.textContent = revealed >= 3 ? 'All clues revealed' : 'Reveal next clue';
    detectiveControls.classList.add('show');
  } else {
    detectiveControls.classList.remove('show');
    detectiveMeta.textContent = '';
    detectiveBtn.disabled = true;
    detectiveBtn.textContent = 'Reveal next clue';
  }
}

function renderScores(state) {
  const STREAK_REQUIRED = 3;
  const guessedPlayers = new Set((state.round_guesses || []).map(guess => guess.player_index));
  document.getElementById('scores-list').innerHTML = state.players.map((p, i) => {
    const fire = p.streak >= STREAK_REQUIRED ? ' 🔥' : p.streak === STREAK_REQUIRED - 1 ? ' 🌡️' : '';
    let tag = 'Waiting';
    if (state.phase === 'category_pick') {
      tag = i === state.current_picker_index ? 'Picker' : 'Ready';
    } else if (state.phase === 'guessing') {
      tag = guessedPlayers.has(i) ? '✓ Locked in' : 'Guessing…';
    } else if (state.phase === 'results' || state.phase === 'finished') {
      tag = 'Done';
    }
    return `<div class="score-row">
      <div class="mini-dot" style="background:${p.color}"></div>
      <div class="score-name">${p.name}${fire}</div>
      <div class="score-val">${p.total_score.toLocaleString()}</div>
      <div class="score-tag">${tag}</div>
    </div>`;
  }).join('');
}

function renderJokers(state) {
  const dBtn = document.getElementById('joker-double-btn');
  const pBtn = document.getElementById('joker-peek-btn');

  if (!state || state.phase !== 'guessing') {
    dBtn.disabled = true;
    pBtn.disabled = true;
    dBtn.style.opacity = '0.35';
    pBtn.style.opacity = '0.35';
    return;
  }

  const p = state.players[state.viewer_index];
  const canUse = state.can_guess;
  const doubleActive = Boolean(p.joker_double_active);

  dBtn.disabled = doubleActive || !canUse || !p.joker_double;
  pBtn.disabled = !canUse || !p.joker_peek;
  dBtn.innerHTML = doubleActive
    ? '×2 ACTIVE this round!'
    : p.joker_double
    ? '×2 Double Points<span class="joker-tag">Doubles this round\'s score</span>'
    : '×2 Used';
  pBtn.innerHTML = p.joker_peek
    ? '👁 Peek (3s)<span class="joker-tag">Shows the target area</span>'
    : '👁 Used';
  dBtn.style.opacity = dBtn.disabled ? '0.35' : '';
  pBtn.style.opacity = pBtn.disabled ? '0.35' : '';
  dBtn.classList.toggle('active-joker', doubleActive);
}

function renderGameChrome(state) {
  const modeLabels = {
    country_flag: '🚩 Country Flag',
    country_capital: '🏙️ Country Capital',
    country_stats: '📊 Country Facts',
    country_landmark: '🏛️ Landmark',
    city_name: '📍 Find the City',
    city_facts: '🧠 City Facts',
    detective_city: '🕵️ Detective City',
  };

  document.getElementById('round-lbl').textContent = `Round ${state.current_round} of ${state.rounds}`;
  document.getElementById('round-counter').textContent = `${state.current_round} / ${state.rounds}`;
  document.getElementById('mode-chip').textContent = modeLabels[state.question?.mode] || 'Waiting';

  if (state.phase === 'category_pick') {
    const picker = state.players[state.current_picker_index];
    document.getElementById('current-player-dot').style.background = picker.color;
    document.getElementById('current-player-label').textContent = `${picker.name} chooses the mode`;
    document.getElementById('streak-fire').style.display = picker.streak >= 3 ? 'inline' : 'none';
    document.getElementById('turn-sub').textContent = state.can_choose_category
      ? 'You are the round picker — choose the next clue type.'
      : `Waiting for ${picker.name} to choose the category.`;
  } else if (state.phase === 'guessing') {
    const viewer = state.players[state.viewer_index];
    const submitted = (state.round_guesses || []).length;
    const total = state.players.length;
    document.getElementById('current-player-dot').style.background = viewer?.color || '#aaa';
    document.getElementById('current-player-label').textContent = state.can_guess
      ? 'Your turn — guess now!'
      : 'Your guess is locked in!';
    document.getElementById('streak-fire').style.display = (viewer?.streak >= 3) ? 'inline' : 'none';
    document.getElementById('turn-sub').textContent = state.can_guess
      ? 'Place your marker and confirm before time runs out.'
      : `${submitted} of ${total} guesses submitted — waiting for others…`;
  } else {
    const viewer = state.players[state.viewer_index];
    document.getElementById('current-player-dot').style.background = viewer?.color || '#aaa';
    document.getElementById('current-player-label').textContent = viewer?.name || '';
    document.getElementById('streak-fire').style.display = 'none';
    document.getElementById('turn-sub').textContent = `Room ${state.room_code} · online multiplayer`;
  }

  renderClue(state.question);
  renderScores(state);
  renderJokers(state);
}

function setGuessControlsForState(state) {
  const confirmBtn = document.getElementById('confirm-btn');
  if (state.phase !== 'guessing') {
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Waiting…';
    document.getElementById('map-hint').textContent = state.phase === 'category_pick'
      ? 'The next round begins once a category is chosen.'
      : 'Waiting for the next phase.';
    document.getElementById('ctrl-note').textContent = 'Online room synced through the Python backend.';
    return;
  }

  if (state.can_guess) {
    confirmBtn.disabled = !pendingGuess;
    confirmBtn.textContent = pendingGuess ? 'Confirm Guess' : 'Click the map first';
    document.getElementById('map-hint').textContent = pendingGuess
      ? 'Marker placed — use jokers or confirm.'
      : 'Click anywhere on the map to place your guess.';
    document.getElementById('ctrl-note').textContent = state.question?.mode === 'detective_city'
      ? 'Use 1 clue for ×3, 2 clues for ×2, all 3 clues for ×1.'
      : 'Results appear only after everybody has guessed.';
  } else {
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Guess submitted ✓';
    document.getElementById('map-hint').textContent = 'Your guess is locked in — waiting for others.';
    document.getElementById('ctrl-note').textContent = 'All players guess simultaneously. Results appear once everyone has submitted.';
  }
}

function showCategoryPicker(state) {
  const picker = state.players[state.current_picker_index];
  const grid = document.getElementById('cat-grid');

  document.getElementById('cat-dot').style.background = picker.color;
  if (state.can_choose_category) {
    document.getElementById('cat-title').textContent = `${picker.name} — choose a category`;
    document.getElementById('cat-sub').textContent = `Round ${state.current_round} of ${state.rounds} · Pick the clue type.`;

    grid.innerHTML = '';
    CATEGORY_DEFS.forEach(cat => {
      const btn = document.createElement('button');
      btn.className = 'cat-btn';
      btn.innerHTML = `
        <span class="cat-icon">${cat.icon}</span>
        <span class="cat-name">${cat.name}</span>
        <span class="cat-desc">${cat.desc}</span>
      `;
      btn.addEventListener('click', async () => {
        const payload = await api('/api/category', 'POST', { mode: cat.mode });
        if (payload) applyPayload(payload);
      });
      grid.appendChild(btn);
    });
  } else {
    document.getElementById('cat-title').textContent = `${picker.name} is choosing the next mode`;
    document.getElementById('cat-sub').textContent = 'Waiting for the round picker to make the call.';
    grid.innerHTML = `
      <div class="cat-btn" style="grid-column:1 / -1; cursor:default; opacity:.9;">
        <span class="cat-icon">⏳</span>
        <span class="cat-name">Please wait</span>
        <span class="cat-desc">The category selection will appear here automatically once it is locked in.</span>
      </div>
    `;
  }

  document.getElementById('cat-overlay').classList.add('active');
}

function hideCategoryPicker() {
  document.getElementById('cat-overlay').classList.remove('active');
}

// ─── Main state processor ────────────────────────────────────────────────────
function processState(state) {
  initGameMap();

  const previousVersion = currentState?.version;
  const previousPhase = currentState?.phase;
  const previousCanGuess = currentState?.can_guess;
  const versionChanged = previousVersion !== state.version;
  const phaseChanged = previousPhase !== state.phase;

  currentState = state;

  if (versionChanged && state.perfect_event) {
    const key = `${state.version}:${state.perfect_event.player_name}`;
    if (lastPerfectEventKey !== key) {
      lastPerfectEventKey = key;
      triggerPerfect(state.perfect_event.player_name);
    }
  }
  if (versionChanged && state.streak_event) {
    const key = `${state.version}:${state.streak_event.player_name}`;
    if (lastStreakEventKey !== key) {
      lastStreakEventKey = key;
      showStreakBanner(state.streak_event.player_name);
    }
  }

  if (phaseChanged || versionChanged) {
    renderGameChrome(state);
  }

  if (state.phase === 'category_pick') {
    showScreen('screen-game');
    stopTimer();
    clearGuessMarker();
    if (peekLayer && gameMap) { gameMap.removeLayer(peekLayer); peekLayer = null; }
    document.getElementById('peek-overlay').style.display = 'none';
    showCategoryPicker(state);
    setGuessControlsForState(state);
    invalidateMapsSoon();
    activeResultsKey = null;
    activeFinalKey = null;
    return;
  }

  hideCategoryPicker();

  if (state.phase === 'guessing') {
    showScreen('screen-game');
    invalidateMapsSoon();

    // Clear marker when phase changes or when THIS player just submitted (can_guess flipped to false)
    if (phaseChanged || (previousCanGuess && !state.can_guess)) {
      clearGuessMarker();
    }

    if (peekLayer && gameMap && previousCanGuess && !state.can_guess) {
      gameMap.removeLayer(peekLayer);
      peekLayer = null;
      document.getElementById('peek-overlay').style.display = 'none';
    }

    setGuessControlsForState(state);
    startTimer(state.turn_ends_at_ms, state.can_guess);
    activeResultsKey = null;
    activeFinalKey = null;
    return;
  }

  if (state.phase === 'results') {
    stopTimer();
    clearGuessMarker();
    const resultsKey = `${state.current_round}:${state.version}`;
    if (activeResultsKey !== resultsKey) {
      activeResultsKey = resultsKey;
      showResultsScreen(state);
    }
    activeFinalKey = null;
    return;
  }

  if (state.phase === 'finished') {
    stopTimer();
    clearGuessMarker();
    const finalKey = `${state.current_round}:${state.version}`;
    if (activeFinalKey !== finalKey) {
      activeFinalKey = finalKey;
      showFinalScreen(state);
    }
  }
}

// ─── Results / reveal ────────────────────────────────────────────────────────
async function showResultsScreen(state) {
  showScreen('screen-results');
  initResultsMap();
  clearRevealLayers();
  invalidateMapsSoon();

  const q = state.question;
  document.getElementById('reveal-answer').textContent = `📍 ${q.answer_name}`;
  document.getElementById('reveal-step').textContent = 'Revealing target…';
  document.getElementById('results-rnd').textContent = `Round ${state.current_round} of ${state.rounds}`;
  document.getElementById('next-round-btn').disabled = true;
  document.getElementById('next-round-btn').textContent = state.can_advance_round ? 'Please wait…' : 'Waiting for host…';

  const valid = state.round_guesses.filter(g => !g.timed_out);
  const closest = valid.length ? valid.reduce((best, g) => g.distance_km < best.distance_km ? g : best) : null;

  const cardsEl = document.getElementById('result-cards');
  cardsEl.innerHTML = '';
  const sorted = [...state.round_guesses].sort((a, b) => b.round_score - a.round_score);
  const cardEls = [];

  sorted.forEach(g => {
    const isClosest = closest && g.player_index === closest.player_index && !g.timed_out;
    const isPerfect = g.inside_target;
    const total = g.total_after_round ?? state.players[g.player_index].total_score;
    const pct = g.timed_out ? 0 : Math.max(3, Math.round((1 - g.distance_km / MAX_DISTANCE_KM) * 100));

    const distNum = g.timed_out ? '❌' : isPerfect ? '0 km' : `${g.distance_km?.toLocaleString()} km`;
    const distLbl = g.timed_out ? 'Timed out — phantom in ocean' : isPerfect ? '🎯 Inside the target area!' : 'from the target';

    const badges = [];
    if (isClosest && !isPerfect) badges.push('<span class="rc-badge badge-winner">🏆 Closest</span>');
    if (isPerfect) badges.push('<span class="rc-badge badge-perfect">🎯 Perfect</span>');
    if (g.joker_double) badges.push('<span class="rc-badge badge-double">×2 Joker</span>');
    if ((g.detective_multiplier ?? 1) > 1) badges.push(`<span class="rc-badge badge-double">🕵️ ×${g.detective_multiplier}</span>`);
    if ((state.players[g.player_index].streak ?? 0) >= 3) badges.push('<span class="rc-badge badge-streak">🔥 Streak</span>');
    if (g.timed_out) badges.push('<span class="rc-badge badge-phantom">Phantom</span>');
    const detectiveNote = q.mode === 'detective_city'
      ? `<div class="rc-dist-lbl">Detective mode · ${g.detective_clues_used ?? 1} clue(s) used</div>`
      : '';

    const card = document.createElement('div');
    card.className = `result-card${isPerfect ? ' perfect-card' : isClosest ? ' winner-card' : g.timed_out ? ' timeout-card' : ''}`;
    card.innerHTML = `
      <div class="rc-top">
        <span class="rc-dot" style="background:${g.player_color}"></span>
        <span class="rc-name">${g.player_name}</span>
        ${badges.join('')}
      </div>
      <div class="rc-dist">${distNum}</div>
      <div class="rc-dist-lbl">${distLbl}</div>
      <div class="rc-score-row">
        <span class="rc-score${g.round_score < 0 ? ' neg' : ''}">${g.round_score >= 0 ? '+' : '−'}${Math.abs(g.round_score).toLocaleString()}</span>
        <span class="rc-total">pts · ${total.toLocaleString()} total</span>
      </div>
      ${detectiveNote}
      ${!g.timed_out ? `<div class="rc-bar-bg"><div class="rc-bar" id="rbar-${g.player_index}" style="background:${g.player_color}"></div></div>` : ''}
    `;
    cardsEl.appendChild(card);
    cardEls.push({ el: card, pct });
  });

  const bounds = [[q.answer_lat - 2, q.answer_lng - 2], [q.answer_lat + 2, q.answer_lng + 2]];
  const targetStyle = { color: '#f1c40f', weight: 2, fillColor: '#f1c40f', fillOpacity: 0.2 };

  if (q.kind === 'country' && q.answer_iso) {
    try {
      const res = await fetch(`/api/border/${q.answer_iso}`);
      if (res.ok) {
        const geojson = await res.json();
        const layer = L.geoJSON(geojson, { style: targetStyle }).addTo(resultsMap);
        revealLayers.push(layer);
        try {
          const b = layer.getBounds();
          bounds.push(
            [b.getSouthWest().lat, b.getSouthWest().lng],
            [b.getNorthEast().lat, b.getNorthEast().lng],
          );
        } catch (_) {}
      }
    } catch (_) {}
  } else if (q.radius_km) {
    const circle = L.circle([q.answer_lat, q.answer_lng], {
      radius: q.radius_km * 1000, ...targetStyle,
    }).addTo(resultsMap);
    revealLayers.push(circle);
    try {
      const b = circle.getBounds();
      bounds.push([b.getSouthWest().lat, b.getSouthWest().lng], [b.getNorthEast().lat, b.getNorthEast().lng]);
    } catch (_) {}
  }

  const starMarker = L.marker([q.answer_lat, q.answer_lng], {
    icon: L.divIcon({
      className: '',
      html: `<div style="font-size:28px;line-height:1;filter:drop-shadow(0 2px 4px rgba(0,0,0,.6))">⭐</div>`,
      iconSize: [28, 28], iconAnchor: [14, 14],
    }),
  }).addTo(resultsMap).bindPopup(`<strong>${q.answer_name}</strong><br>Target`).openPopup();
  revealLayers.push(starMarker);
  resultsMap.fitBounds(bounds, { padding: [40, 40] });

  await wait(700);

  for (let i = 0; i < sorted.length; i++) {
    const g = sorted[i];
    const cardMeta = cardEls[i];
    cardMeta.el.classList.add('visible');
    Sounds.reveal();
    document.getElementById('reveal-step').textContent = `Revealing ${g.player_name}…`;

    if (g.timed_out) {
      const pm = L.marker([g.phantom_lat, g.phantom_lng], {
        icon: L.divIcon({
          className: '',
          html: `<div style="width:22px;height:22px;border-radius:50%;background:#ff6b6b;border:3px solid white;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;color:white;box-shadow:0 2px 8px rgba(0,0,0,.5)">✕</div>`,
          iconSize: [22, 22], iconAnchor: [11, 11],
        }),
      }).addTo(resultsMap)
        .bindPopup(`<strong>${g.player_name}</strong><br>Timed out! Phantom placed here.<br>−500 pts penalty`)
        .openPopup();
      revealLayers.push(pm);
      document.getElementById('reveal-step').textContent = `❌ ${g.player_name} timed out.`;
    } else {
      const gm = L.marker([g.lat, g.lng], {
        icon: L.divIcon({
          className: '',
          html: `<div style="width:20px;height:20px;border-radius:50%;background:${g.player_color};border:3px solid white;box-shadow:0 2px 10px rgba(0,0,0,.5)"></div>`,
          iconSize: [20, 20], iconAnchor: [10, 10],
        }),
      }).addTo(resultsMap)
        .bindPopup(`<strong>${g.player_name}</strong><br>${g.inside_target ? '🎯 Inside target!' : `${g.distance_km?.toLocaleString()} km away`}<br>+${g.round_score?.toLocaleString()} pts`)
        .openPopup();
      revealLayers.push(gm);

      // Normalize answer lng relative to guess so the line takes the shorter path
      const adjAnswerLng = nearLng(g.lng, q.answer_lng);
      bounds.push([g.lat, g.lng]);
      resultsMap.fitBounds(bounds, { padding: [40, 40], animate: true });
      await wait(450);

      if (!g.inside_target) {
        const line = L.polyline([[g.lat, g.lng], [q.answer_lat, adjAnswerLng]], {
          color: g.player_color, weight: 2.5, opacity: 0.85, dashArray: '8,6',
        }).addTo(resultsMap);
        revealLayers.push(line);

        const midLat = (g.lat + q.answer_lat) / 2;
        const midLng = (g.lng + adjAnswerLng) / 2;
        const distLabel = L.marker([midLat, midLng], {
          icon: L.divIcon({
            className: '',
            html: `<div style="background:${g.player_color};color:white;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.45);border:2px solid rgba(255,255,255,.3)">${g.distance_km?.toLocaleString()} km</div>`,
            iconAnchor: [30, 10],
          }),
        }).addTo(resultsMap);
        revealLayers.push(distLabel);
      } else {
        triggerPerfect(g.player_name);
      }

      const barEl = document.getElementById(`rbar-${g.player_index}`);
      if (barEl) {
        await wait(100);
        barEl.style.width = `${cardMeta.pct}%`;
      }
    }

    await wait(900);
  }

  document.getElementById('reveal-step').textContent = closest
    ? `🏆 ${closest.player_name} was closest — ${closest.distance_km?.toLocaleString()} km away.`
    : 'No valid guesses this round.';

  const nextBtn = document.getElementById('next-round-btn');
  if (state.can_advance_round) {
    nextBtn.disabled = false;
    nextBtn.textContent = state.current_round >= state.rounds ? '🏁 View Final Ranking' : 'Next Round →';
  } else {
    nextBtn.disabled = true;
    nextBtn.textContent = 'Waiting for host…';
  }
}

// ─── Final screen ────────────────────────────────────────────────────────────
function showFinalScreen(state) {
  showScreen('screen-final');
  const ranking = [...state.players].sort((a, b) => b.total_score - a.total_score);

  document.getElementById('final-title').textContent =
    ranking.length > 1 ? `${ranking[0].name} wins! 🎉` : 'Game Over';
  document.getElementById('final-sub').textContent =
    `Final results after ${state.rounds} rounds in room ${state.room_code}.`;

  let podiumSlots;
  if (ranking.length === 1) {
    podiumSlots = [{ player: ranking[0], height: 160, icon: '🥇', isFirst: true }];
  } else if (ranking.length === 2) {
    podiumSlots = [
      { player: ranking[1], height: 110, icon: '🥈', isFirst: false },
      { player: ranking[0], height: 160, icon: '🥇', isFirst: true },
    ];
  } else {
    podiumSlots = [
      { player: ranking[1], height: 120, icon: '🥈', isFirst: false },
      { player: ranking[0], height: 170, icon: '🥇', isFirst: true },
      { player: ranking[2], height: 90, icon: '🥉', isFirst: false },
    ];
  }

  document.getElementById('podium').innerHTML = podiumSlots.map(slot => {
    const glow = slot.isFirst ? `box-shadow:0 0 24px ${slot.player.color}55;` : '';
    const border = slot.isFirst
      ? `border:2px solid ${slot.player.color};`
      : `border:1px solid ${slot.player.color}55;`;
    return `<div class="podium-item${slot.isFirst ? ' podium-winner' : ''}">
      <div class="podium-medal">${slot.icon}</div>
      <div class="podium-name" style="color:${slot.player.color}">${slot.player.name}</div>
      <div class="podium-score">${slot.player.total_score.toLocaleString()} pts</div>
      <div class="podium-bar" style="height:${slot.height}px;background:${slot.player.color}20;${border}${glow}"></div>
    </div>`;
  }).join('');

  const medals = ['🥇', '🥈', '🥉'];
  document.getElementById('final-list').innerHTML = ranking.map((p, i) => {
    const extras = [];
    if (p.streak_bonus_earned > 0) extras.push(`🔥 +${p.streak_bonus_earned} streak`);
    if (p.best_round_km !== null && p.best_round_km !== undefined) extras.push(`Best: ${p.best_round_km.toLocaleString()} km`);
    const isWinner = i === 0 && ranking.length > 1;
    return `<div class="final-row${isWinner ? ' final-row-winner' : ''}">
      <div class="final-rank">${medals[i] || '#' + (i + 1)}</div>
      <div class="mini-dot" style="background:${p.color}"></div>
      <div class="final-name">${p.name}</div>
      <div class="final-extra">${extras.join(' · ')}</div>
      <div class="final-score">${p.total_score.toLocaleString()} pts</div>
    </div>`;
  }).join('');

  if (ranking.length > 1) { spawnConfetti(); Sounds.fanfare(); }
  updateRematchButton();
}

function updateRematchButton() {
  if (!currentRoom) return;
  const btn = document.getElementById('play-again-btn');
  const votes = currentRoom.rematch_votes ?? 0;
  const total = currentRoom.rematch_total ?? 1;
  const voted = currentRoom.viewer_voted_rematch ?? false;

  if (voted) {
    btn.disabled = true;
    btn.textContent = votes >= total ? 'Starting…' : `Voted ✓  (${votes}/${total} ready)`;
  } else {
    btn.disabled = false;
    btn.textContent = total === 1 ? 'Play Again' : `Play Again  (${votes}/${total} ready)`;
  }
}

// ─── Animations ──────────────────────────────────────────────────────────────
function triggerPerfect(playerName) {
  document.getElementById('perfect-name').textContent = `${playerName} landed right on target — 5,000 pts!`;
  const el = document.getElementById('perfect-overlay');
  el.classList.add('show');
  Sounds.perfect();
  spawnConfetti();
  setTimeout(() => el.classList.remove('show'), 3000);
}

function showStreakBanner(playerName) {
  const el = document.getElementById('streak-banner');
  el.querySelector('#streak-name').textContent = playerName;
  el.classList.add('show');
  Sounds.streak();
  setTimeout(() => el.classList.remove('show'), 2800);
}

function spawnConfetti() {
  const c = document.getElementById('confetti-wrap');
  c.innerHTML = '';
  const colors = ['#e74c3c', '#3498db', '#2ecc71', '#f1c40f', '#9b59b6', '#e67e22', '#ff6b6b'];
  for (let i = 0; i < 80; i++) {
    const p = document.createElement('div');
    p.className = 'confetti-piece';
    const size = 6 + Math.random() * 8;
    p.style.cssText = `left:${Math.random() * 100}%;width:${size}px;height:${size}px;background:${colors[Math.floor(Math.random() * colors.length)]};border-radius:${Math.random() > .5 ? '50%' : '2px'};animation-duration:${1.5 + Math.random() * 2}s;animation-delay:${Math.random() * .8}s;transform:rotate(${Math.random() * 360}deg);`;
    c.appendChild(p);
  }
  setTimeout(() => { c.innerHTML = ''; }, 4000);
}

// ─── Jokers / detective actions ──────────────────────────────────────────────
async function activatePeek() {
  if (!currentState || !currentState.can_guess) return;
  const payload = await api('/api/joker/peek', 'POST', {});
  if (!payload) return;
  applyPayload(payload);

  const target = payload.peek_target;
  if (!target) return;

  if (peekLayer && gameMap) { gameMap.removeLayer(peekLayer); peekLayer = null; }

  const peekStyle = { color: '#9b59b6', weight: 3, fillColor: '#9b59b6', fillOpacity: 0.22, dashArray: '6,4' };

  if (target.kind === 'country' && target.iso) {
    try {
      const res = await fetch(`/api/border/${target.iso}`);
      if (res.ok) {
        const geojson = await res.json();
        peekLayer = L.geoJSON(geojson, { style: peekStyle }).addTo(gameMap);
      }
    } catch (_) {}
  }

  if (!peekLayer) {
    peekLayer = L.circle([target.lat, target.lng], {
      radius: target.radius_km * 1000, ...peekStyle,
    }).addTo(gameMap);
  }

  const overlay = document.getElementById('peek-overlay');
  const count = document.getElementById('peek-count');
  overlay.style.display = 'block';
  let t = 3;
  count.textContent = t;

  const iv = setInterval(() => {
    t -= 1;
    if (t <= 0) {
      clearInterval(iv);
      if (peekLayer && gameMap) { gameMap.removeLayer(peekLayer); peekLayer = null; }
      overlay.style.display = 'none';
      document.getElementById('map-hint').textContent = 'Outline hidden — now guess.';
    } else {
      count.textContent = t;
    }
  }, 1000);

  document.getElementById('map-hint').textContent = '👁 Peek active — memorize the target area.';
}

async function activateDouble() {
  if (!currentState || !currentState.can_guess) return;
  const payload = await api('/api/joker/double', 'POST', {});
  if (!payload) return;
  applyPayload(payload);
  const btn = document.getElementById('joker-double-btn');
  btn.textContent = '×2 ACTIVE this round!';
  btn.classList.add('active-joker');
  document.getElementById('ctrl-note').textContent = '×2 is active — your score will be doubled.';
}

async function revealDetectiveHint() {
  if (!currentState || currentState.question?.mode !== 'detective_city' || !currentState.can_guess) return;
  const payload = await api('/api/detective/hint', 'POST', {});
  if (!payload) return;
  applyPayload(payload);
  if (payload.game?.detective_progress) {
    document.getElementById('ctrl-note').textContent =
      `Detective mode active — ${payload.game.detective_progress.revealed_clues} clue(s) used, multiplier ×${payload.game.detective_progress.score_multiplier}.`;
  }
}

// ─── Utilities ───────────────────────────────────────────────────────────────

// Normalize targetLng so the shorter arc relative to refLng is used (antimeridian fix).
function nearLng(refLng, targetLng) {
  let lng = targetLng;
  while (lng - refLng > 180) lng -= 360;
  while (refLng - lng > 180) lng += 360;
  return lng;
}

function flagEmoji(iso) {
  return iso.toUpperCase().split('').map(c => String.fromCodePoint(c.charCodeAt(0) + 127397)).join('');
}

function showScreen(id) {
  document.querySelectorAll('.screen').forEach(screen => screen.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

let toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2500);
}

function clearRevealLayers() {
  revealLayers.forEach(layer => {
    try { resultsMap.removeLayer(layer); } catch (_) {}
  });
  revealLayers = [];
}

function getSelectedLobbyRounds() {
  return Number(document.querySelector('#lobby-round-options .round-option.active')?.dataset.rounds ?? 5);
}

function prefillRoomCodeFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const room = params.get('room');
  if (!room) return;

  document.getElementById('join-room-code').value = room.toUpperCase();

  // Invite mode: hide create section, highlight join section
  document.getElementById('create-section').style.display = 'none';
  document.getElementById('setup-heading').textContent = 'You were invited to a room';
  document.getElementById('start-note').textContent = 'Enter your name and join the room. The host will start the match once everyone is in.';

  const joinSection = document.getElementById('join-section');
  joinSection.style.cssText = 'background:rgba(78,161,255,0.08);border:1px solid rgba(78,161,255,0.25);border-radius:16px;padding:16px;';

  const joinBtn = document.getElementById('join-room-btn');
  joinBtn.className = 'primary-btn';
  joinBtn.style.width = '100%';
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

// ─── Room actions ────────────────────────────────────────────────────────────
async function createRoom() {
  const name = document.getElementById('create-name').value.trim();
  if (!name) {
    showToast('Enter your player name first.');
    return;
  }

  const btn = document.getElementById('create-room-btn');
  btn.disabled = true;
  btn.textContent = 'Creating…';
  const payload = await api('/api/room/create', 'POST', { name });
  btn.disabled = false;
  btn.textContent = 'Create Room';
  if (payload) applyPayload(payload);
}

async function joinRoom() {
  const name = document.getElementById('join-name').value.trim();
  const roomCode = document.getElementById('join-room-code').value.trim().toUpperCase();
  if (!name || !roomCode) {
    showToast('Enter your name and room code.');
    return;
  }

  const btn = document.getElementById('join-room-btn');
  btn.disabled = true;
  btn.textContent = 'Joining…';
  const payload = await api('/api/room/join', 'POST', { name, room_code: roomCode });
  btn.disabled = false;
  btn.textContent = 'Join Room';
  if (payload) applyPayload(payload);
}

async function startMatch() {
  const payload = await api('/api/start', 'POST', { rounds: getSelectedLobbyRounds() });
  if (payload) applyPayload(payload);
}

async function leaveRoom() {
  const payload = await api('/api/room/leave', 'POST', {});
  if (payload) applyPayload(payload);
}

async function copyInviteLink() {
  if (!currentRoom) return;
  const url = `${window.location.origin}${window.location.pathname}?room=${currentRoom.room_code}`;
  try {
    await navigator.clipboard.writeText(url);
    showToast('Invite link copied.');
  } catch (_) {
    showToast(url);
  }
}

// ─── Event listeners ─────────────────────────────────────────────────────────
document.getElementById('create-room-btn').addEventListener('click', createRoom);
document.getElementById('join-room-btn').addEventListener('click', joinRoom);
document.getElementById('copy-room-btn').addEventListener('click', copyInviteLink);
document.getElementById('lobby-start-btn').addEventListener('click', startMatch);
document.getElementById('leave-room-btn').addEventListener('click', leaveRoom);

document.getElementById('lobby-round-options').addEventListener('click', async e => {
  const btn = e.target.closest('[data-rounds]');
  if (!btn || (currentRoom && !currentRoom.is_host)) return;
  const rounds = Number(btn.dataset.rounds);
  const payload = await api('/api/room/rounds', 'POST', { rounds });
  if (payload) applyPayload(payload);
});

document.getElementById('confirm-btn').addEventListener('click', async () => {
  if (!pendingGuess || !currentState?.can_guess) return;
  const confirmBtn = document.getElementById('confirm-btn');
  confirmBtn.disabled = true;

  if (peekLayer && gameMap) { gameMap.removeLayer(peekLayer); peekLayer = null; }
  document.getElementById('peek-overlay').style.display = 'none';

  Sounds.guess();
  const payload = await api('/api/guess', 'POST', pendingGuess);
  if (payload) applyPayload(payload);
});

document.getElementById('joker-double-btn').addEventListener('click', activateDouble);
document.getElementById('joker-peek-btn').addEventListener('click', activatePeek);
document.getElementById('detective-hint-btn').addEventListener('click', revealDetectiveHint);

document.getElementById('next-round-btn').addEventListener('click', async () => {
  clearRevealLayers();
  const payload = await api('/api/next-round', 'POST', {});
  if (payload) applyPayload(payload);
});

document.getElementById('play-again-btn').addEventListener('click', async () => {
  const payload = await api('/api/rematch', 'POST', {});
  if (payload) applyPayload(payload);
});

document.getElementById('back-start-btn').addEventListener('click', async () => {
  const payload = await api('/api/room/leave', 'POST', {}, { showErrors: false });
  if (payload) applyPayload(payload);
});

window.addEventListener('load', async () => {
  prefillRoomCodeFromQuery();
  await refreshFromServer();
  const muteBtn = document.getElementById('mute-btn');
  muteBtn.textContent = Sounds.isMuted() ? '🔇' : '🔊';
  muteBtn.addEventListener('click', () => {
    const muted = Sounds.toggleMute();
    muteBtn.textContent = muted ? '🔇' : '🔊';
  });
});
window.addEventListener('resize', invalidateMapsSoon);
