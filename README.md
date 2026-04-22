# GeoMaster — Flask Edition

A geography guessing game where **all game logic runs in Python**.

## Folder structure

```
geomaster/
├── app.py           ← Flask routes (API endpoints)
├── game_logic.py    ← All game logic in Python
├── data.py          ← Countries, cities, dataset
├── requirements.txt
├── templates/
│   └── index.html   ← HTML (Jinja2 template)
└── static/
    ├── style.css    ← Styles
    └── app.js       ← Map display + API calls only
```

## What Python does
- Creates and stores game state (Flask session)
- Generates questions, picks random countries/cities
- Calculates distances (Haversine formula)
- Calculates scores (linear: 0 km = 5000 pts, 10 000 km = 0 pts)
- Manages streaks and streak bonuses
- Handles jokers (×2 double points, peek)
- Phantom guess + penalty on timeout
- Multiplayer turn management
- Hides other players' guesses until all have submitted

## What JavaScript does
- Displays the Leaflet map
- Sends API calls to Python
- Runs the client-side timer
- Draws markers, lines and animations
- Shows overlays (pass device, category picker, confetti)

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the server
python app.py

# 3. Open in browser
http://127.0.0.1:5000
```

## API endpoints

| Method | URL | What it does |
|--------|-----|-------------|
| POST | /api/start | Start a new game |
| GET | /api/state | Get current state |
| POST | /api/category | Player picks a clue mode |
| POST | /api/guess | Submit a lat/lng guess |
| POST | /api/timeout | Timer expired — phantom guess |
| POST | /api/next-round | Advance to next round |
| POST | /api/joker/double | Activate ×2 joker |
| POST | /api/joker/peek | Use peek joker |

## Edge cases handled in Python
- Coordinates out of valid range → rejected with error message
- Negative total score → clamped to 0
- No players entered → error returned
- Invalid joker mode sent → error returned  
- Category mode not in allowed list → error returned
- Game already finished → phase set to "finished"
- All countries used → reshuffled automatically
