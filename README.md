# GeoMaster

**Live:** https://geomasterdeluxe.onrender.com/
**GitHub:** [lensbums-ops/GeoMaster](https://github.com/lensbums-ops/GeoMaster)

A real-time multiplayer geography guessing game. All game logic runs in Python; the browser handles map display and API calls.

## Features

- **Simultaneous multiplayer** — all players guess at the same time, no waiting for turns
- **Room-based online play** — create a room, share the code or invite link, play from separate browsers
- **7 question modes** — Country Flag, Country Capital, Country Stats, Country Landmark, City Name, City Facts, Detective City
- **Real country borders** — country scoring uses actual polygon boundaries (Natural Earth 110 m) when available, not just a radius circle
- **30-second timer** — placed markers are auto-submitted when time runs out; no guess = phantom + penalty
- **Streaks** — 3 consecutive rounds under 1 000 km earns +500 pts and a fire indicator
- **Jokers** — each player gets one Double Points (×2) and one Peek (shows target border for 3 s) per game
- **Rematch voting** — all remaining players must vote to replay; disconnected players are excluded
- **Mid-game leave** — players can leave at any point; their turn is auto-phantomed and the game continues
- **Room TTL** — rooms inactive for 5 minutes are automatically deleted from server memory
- **Invite links** — `/?room=XXXXX` pre-fills the join form and collapses the create section
- **Synthesised sound effects** — Web Audio API, no external files; mute toggle persisted in localStorage
- **Antimeridian fix** — map lines always take the short route (no wrap-around the date line)
- **Clean result reveal** — animated answer star, answer label, guessed-country labels, distance lines, and result cards

## Folder structure

```
geomaster/
├── app.py                  ← Flask routes and room management
├── game_logic.py           ← All scoring, phases, jokers, streaks
├── data.py                 ← Countries, cities, detective cities dataset
├── prepare_borders.py      ← One-time script to generate country_borders.json
├── country_borders.json    ← Natural Earth polygon data (172 countries)
├── requirements.txt
├── templates/
│   └── index.html          ← Jinja2 template (start, lobby, game, results, final screens)
└── static/
    ├── style.css
    ├── app.js              ← Map display, polling loop, UI transitions
    └── sounds.js           ← Synthesised sound effects (Web Audio API)
```

## What Python does

- Stores all room and game state in memory (`ROOMS` dict)
- Generates questions, picks from shuffled pools (no repeats within a game)
- Haversine distance + real polygon distance via Shapely
- Linear scoring: 0 km → 5 000 pts, 10 000 km → 0 pts; inside target → 5 000 pts
- Streak tracking and bonus awards
- Joker logic (×2 double, peek)
- Placed markers are auto-confirmed on timeout; no marker means phantom + penalty
- Hides other players' guesses until everyone has submitted
- Cleans up stale rooms automatically

## What JavaScript does

- Leaflet map: markers, polylines, target border/peek overlays, answer labels
- 1.5 s polling loop (`/api/state`) drives all screen transitions
- Client-side countdown timer (synced to server deadline)
- Sound effects and mute toggle
- Invite-link mode (URL query param `?room=`)

## Local setup

### macOS

1. Open **Terminal** (`Cmd + Space` → "Terminal")
2. Navigate to the project folder: `cd path/to/geomaster`
3. Install dependencies: `python3 -m pip install -r requirements.txt`
4. Start the server: `PORT=8080 python3 app.py`
   > macOS reserves port 5000 for AirPlay — use 8080 instead.
5. Open `http://localhost:8080` in your browser.

### Windows

1. Open **Command Prompt** or **PowerShell**
2. Navigate to the project folder: `cd path\to\geomaster`
3. Install dependencies: `pip install -r requirements.txt`
   (If `pip` not found: `python -m pip install -r requirements.txt`)
4. Start the server: `python app.py`
5. Open `http://localhost:5000` in your browser.

### Testing multiplayer locally

Open the game in two browser windows:
- **Window 1** (normal): create a room, note the room code
- **Window 2** (incognito): join using that code
- Click **Start** in Window 1

## Deploy to Render

1. Push the project to GitHub (make sure `country_borders.json` is committed).
2. Create a new **Web Service** from the repo.
3. Use these settings:

```
Build Command:  pip install -r requirements.txt
Start Command:  gunicorn app:app
```

4. Add an environment variable:

```
SECRET_KEY=choose-a-long-random-secret
```

5. Deploy and open the generated `.onrender.com` URL.

> Rooms are stored in server memory and reset on redeploy or restart.

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/bootstrap` | Initial state load on page open |
| GET | `/api/state` | Polling endpoint (every 1.5 s) |
| POST | `/api/room/create` | Create a room and become host |
| POST | `/api/room/join` | Join an existing room by code |
| POST | `/api/room/leave` | Leave room or disconnect mid-game |
| POST | `/api/room/rounds` | Host sets round count in lobby |
| POST | `/api/start` | Host starts the match |
| POST | `/api/category` | Picker chooses question mode |
| POST | `/api/guess` | Submit a lat/lng guess |
| POST | `/api/timeout` | Client-side timeout; confirms placed marker or applies phantom penalty |
| POST | `/api/detective/hint` | Reveal next detective clue |
| POST | `/api/next-round` | Host advances to next round |
| POST | `/api/joker/double` | Activate ×2 joker |
| POST | `/api/joker/peek` | Use peek joker |
| POST | `/api/rematch` | Cast a rematch vote |
| GET | `/api/border/<iso>` | Country polygon GeoJSON by ISO code |
