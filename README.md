# GeoMaster

A real-time multiplayer geography guessing game. All game logic runs in Python; the browser handles map display and API calls.

## Features

- **Simultaneous multiplayer** — all players guess at the same time, no waiting for turns
- **Room-based online play** — create a room, share the code or invite link, play from separate browsers
- **6 question modes** — Country Flag, Country Capital, Country Stats, Country Landmark, City Name, Detective City
- **Real country borders** — scoring uses actual polygon boundaries (Natural Earth 110 m), not just a radius circle
- **30-second timer** — placed markers are auto-submitted when time runs out; no guess = phantom + penalty
- **Streaks** — 3 consecutive rounds under 1 000 km earns +500 pts and a fire indicator
- **Jokers** — each player gets one Double Points (×2) and one Peek (shows target border for 3 s) per game
- **Rematch voting** — all remaining players must vote to replay; disconnected players are excluded
- **Mid-game leave** — players can leave at any point; their turn is auto-phantomed and the game continues
- **Room TTL** — rooms inactive for 5 minutes are automatically deleted from server memory
- **Invite links** — `/?room=XXXXX` pre-fills the join form and collapses the create section
- **Synthesised sound effects** — Web Audio API, no external files; mute toggle persisted in localStorage
- **Antimeridian fix** — map lines always take the short route (no wrap-around the date line)

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
- Phantom guess + penalty on timeout or disconnect
- Hides other players' guesses until everyone has submitted
- Cleans up stale rooms automatically

## What JavaScript does

- Leaflet map: markers, polylines, country polygon overlays
- 1.5 s polling loop (`/api/state`) drives all screen transitions
- Client-side countdown timer (synced to server deadline)
- Sound effects and mute toggle
- Invite-link mode (URL query param `?room=`)

## Local setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Generate country border polygons (only needed once)
python3 prepare_borders.py

# 3. Run the server
python3 app.py

# 4. Open in browser
http://127.0.0.1:5000
```

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
| POST | `/api/timeout` | Client-side timeout (phantom guess) |
| POST | `/api/detective/hint` | Reveal next detective clue |
| POST | `/api/next-round` | Host advances to next round |
| POST | `/api/joker/double` | Activate ×2 joker |
| POST | `/api/joker/peek` | Use peek joker |
| POST | `/api/rematch` | Cast a rematch vote |
| GET | `/api/border/<iso>` | Country polygon GeoJSON by ISO code |
