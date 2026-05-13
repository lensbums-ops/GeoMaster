"""GeoMaster — complete game logic in Python.

All scoring, streaks, jokers, phantom guesses, category selection,
and state transitions live here. The frontend only handles display.
"""
from __future__ import annotations

import json
import math
import os
import random
from copy import deepcopy
from typing import Any

# Optional Shapely for polygon-based country borders
try:
    from shapely.geometry import Point, shape
    from shapely.ops import nearest_points as _shapely_nearest
    _SHAPELY = True
except ImportError:
    _SHAPELY = False

_COUNTRY_BORDERS: dict[str, Any] = {}

def _load_country_borders() -> None:
    if not _SHAPELY:
        return
    path = os.path.join(os.path.dirname(__file__), "country_borders.json")
    try:
        with open(path) as f:
            raw = json.load(f)
        for iso, geom in raw.items():
            _COUNTRY_BORDERS[iso] = shape(geom)
        print(f"[GeoMaster] Loaded {len(_COUNTRY_BORDERS)} country border polygons")
    except FileNotFoundError:
        print("[GeoMaster] country_borders.json not found — using radius fallback")
    except Exception as exc:
        print(f"[GeoMaster] Border load error: {exc}")

_load_country_borders()

from data import (
    CITIES,
    COUNTRIES,
    DETECTIVE_CITIES,
    OCEAN_POINTS,
    PLAYER_COLORS,
    QUESTION_MODES,
)

COUNTRY_NAMES_BY_ISO = {
    country["iso"].lower(): country["name"]
    for country in COUNTRIES
    if country.get("iso") and country.get("name")
}

# Constants
MAX_DISTANCE_KM = 10_000   # 0 km = 5000 pts, 10000 km = 0 pts (linear)
STREAK_THRESHOLD_KM = 1_000  # under this = good round for streak
STREAK_REQUIRED = 3          # rounds in a row needed to earn bonus
STREAK_BONUS_PTS = 500
TIMEOUT_PENALTY_PTS = 500
DETECTIVE_MAX_CLUES = 3
DETECTIVE_MULTIPLIERS = {1: 1.5, 2: 1.0, 3: 0.75}


# Maths helpers
def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    """Great-circle distance in km between two lat/lng points."""
    r = 6_371
    lat1_r, lng1_r, lat2_r, lng2_r = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2_r - lat1_r
    dlng = lng2_r - lng1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2
    return round(r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def score_from_distance(distance_km: int) -> int:
    """Linear scoring: 0 km → 5000 pts, 10 000 km → 0 pts."""
    return max(0, round(5_000 * (1 - distance_km / MAX_DISTANCE_KM)))


def distance_to_zone(
    lat: float, lng: float,
    answer_lat: float, answer_lng: float,
    radius_km: int,
) -> tuple[int, bool]:
    """Return (effective_distance_km, inside_target).

    If the guess lands inside the target radius the effective distance is 0.
    """
    # Edge case: radius_km must be positive
    radius_km = max(1, radius_km)
    center_dist = haversine_km(lat, lng, answer_lat, answer_lng)
    if center_dist <= radius_km:
        return 0, True
    return center_dist - radius_km, False


def _distance_to_country(lat: float, lng: float, iso: str) -> tuple[int | None, bool]:
    """Return (distance_km, inside) using real country border polygon.

    Returns (None, False) when no polygon is available so the caller can
    fall back to the radius-based approach.
    """
    if not iso or not _COUNTRY_BORDERS:
        return None, False

    geom = _COUNTRY_BORDERS.get(iso.lower())
    if geom is None:
        return None, False

    point = Point(lng, lat)  # shapely: (x=lng, y=lat)

    if geom.contains(point):
        return 0, True

    # Nearest point on the polygon boundary in degree space, then haversine
    nearest_pt = _shapely_nearest(geom, point)[0]
    dist_km = haversine_km(lat, lng, nearest_pt.y, nearest_pt.x)
    return dist_km, False


def _country_at_point(lat: float, lng: float) -> dict[str, Any] | None:
    """Return display metadata for the country containing a point, if known."""
    if not _COUNTRY_BORDERS:
        return None

    point = Point(lng, lat)
    for iso, geom in _COUNTRY_BORDERS.items():
        if not geom.covers(point):
            continue

        label_point = geom.representative_point()
        return {
            "iso": iso,
            "name": COUNTRY_NAMES_BY_ISO.get(iso, iso.upper()),
            "label_lat": label_point.y,
            "label_lng": label_point.x,
        }

    nearest_country = None
    nearest_km = None
    for country in COUNTRIES:
        if country.get("kind") != "country":
            continue
        km = haversine_km(lat, lng, country["lat"], country["lng"])
        if km <= country.get("radius_km", 0) and (nearest_km is None or km < nearest_km):
            nearest_country = country
            nearest_km = km

    if nearest_country:
        return {
            "iso": nearest_country["iso"].lower(),
            "name": nearest_country["name"],
            "label_lat": nearest_country["lat"],
            "label_lng": nearest_country["lng"],
        }

    return None


# Game state creation
def create_game_state(players: list[str], rounds: int) -> dict[str, Any]:
    """Create a fresh game state dict."""
    # Edge case: no names provided
    clean = [n.strip() for n in players if n.strip()]
    if not clean:
        raise ValueError("At least one player name is required.")

    # Edge case: rounds must be a reasonable number
    rounds = max(1, min(rounds, 30))

    # Shuffled pools so no country/city repeats within a game
    country_pool = list(range(len(COUNTRIES)))
    city_pool    = list(range(len(CITIES)))
    detective_pool = list(range(len(DETECTIVE_CITIES)))
    random.shuffle(country_pool)
    random.shuffle(city_pool)
    random.shuffle(detective_pool)

    return {
        "players": [
            {
                "name": name,
                "color": PLAYER_COLORS[i],
                "total_score": 0,
                "streak": 0,
                "streak_bonus_earned": 0,
                "best_round_km": None,
                "joker_double": True,
                "joker_peek": True,
            }
            for i, name in enumerate(clean[:4])
        ],
        "rounds": rounds,
        "current_round": 0,
        "current_player_index": 0,
        "phase": "setup",
        "question": None,
        "round_guesses": [],
        "pending_guesses": {},
        "streak_event": None,
        "perfect_event": None,
        "country_pool": country_pool,
        "city_pool": city_pool,
        "detective_pool": detective_pool,
        "detective_progress": None,
    }


# Question generation
def build_question(mode: str, state: dict[str, Any]) -> dict[str, Any]:
    """Build a question dict for the given mode.

    Pops from the state's shuffled pool so no item repeats within a game.
    When the pool is exhausted it is reshuffled automatically.
    """
    if mode == "detective_city":
        pool = state["detective_pool"]
        dataset = DETECTIVE_CITIES
    elif mode.startswith("country_"):
        pool = state["country_pool"]
        dataset = COUNTRIES
    else:
        pool = state["city_pool"]
        dataset = CITIES

    # Edge case: pool exhausted — reshuffle for next cycle
    if not pool:
        pool[:] = list(range(len(dataset)))
        random.shuffle(pool)

    idx = pool.pop(0)
    item = deepcopy(dataset[idx])

    return {
        "mode": mode,
        "item": item,
        "answer_name": item["name"],
        "answer_lat": item["lat"],
        "answer_lng": item["lng"],
        "radius_km": item["radius_km"],
        "kind": item["kind"],
        "answer_iso": item.get("iso"),  # present for country questions
    }


def _fresh_detective_progress() -> dict[str, int | float]:
    return {
        "revealed_clues": 1,
        "score_multiplier": DETECTIVE_MULTIPLIERS[1],
    }


def get_category_picker_index(state: dict[str, Any]) -> int | None:
    """Return the player index currently choosing the category."""
    players = state.get("players", [])
    current_round = state.get("current_round", 0)
    if not players or current_round <= 0:
        return None
    return (current_round - 1) % len(players)


# Round management
def start_round(state: dict[str, Any], chosen_mode: str) -> dict[str, Any]:
    """Begin a round — all players guess simultaneously."""
    state["question"] = build_question(chosen_mode, state)
    state["round_guesses"] = []
    state["pending_guesses"] = {}
    state["streak_event"] = None
    state["perfect_event"] = None
    state["phase"] = "guessing"
    if chosen_mode == "detective_city":
        state["detective_progress"] = {
            i: _fresh_detective_progress() for i in range(len(state["players"]))
        }
    else:
        state["detective_progress"] = None
    return state


def begin_next_round(state: dict[str, Any]) -> dict[str, Any]:
    """Advance round counter and move to category_pick phase."""
    # Edge case: game already over
    if state["current_round"] >= state["rounds"]:
        state["phase"] = "finished"
        return state

    state["current_round"] += 1
    state["phase"] = "category_pick"
    state["question"] = None
    state["round_guesses"] = []
    state["pending_guesses"] = {}
    state["streak_event"] = None
    state["perfect_event"] = None
    state["detective_progress"] = None
    state["results_ends_at_ms"] = None
    return state


def reveal_detective_hint(state: dict[str, Any], player_index: int) -> dict[str, Any]:
    """Reveal one more detective clue for the given player."""
    if state["phase"] != "guessing":
        return state

    question = state.get("question") or {}
    if question.get("mode") != "detective_city":
        return state

    progress_map = state.get("detective_progress") or {}
    progress = progress_map.get(player_index) or _fresh_detective_progress()
    revealed = min(DETECTIVE_MAX_CLUES, progress["revealed_clues"] + 1)
    progress["revealed_clues"] = revealed
    progress["score_multiplier"] = DETECTIVE_MULTIPLIERS[revealed]
    progress_map[player_index] = progress
    state["detective_progress"] = progress_map
    return state


# Jokers
def use_joker_double(state: dict[str, Any], player_index: int | None = None) -> dict[str, Any]:
    """Activate the ×2 joker for the given player."""
    idx = player_index if player_index is not None else state["current_player_index"]
    p = state["players"][idx]

    if not p["joker_double"]:
        return state
    if state["phase"] != "guessing":
        return state

    p["joker_double"] = False
    p["joker_double_active"] = True   # flag consumed in evaluate_guess
    return state


def use_joker_peek(state: dict[str, Any], player_index: int | None = None) -> dict[str, Any]:
    """Mark the peek joker as used for the given player."""
    idx = player_index if player_index is not None else state["current_player_index"]
    p = state["players"][idx]

    if not p.get("joker_peek", False):
        return state
    if state["phase"] != "guessing":
        return state

    p["joker_peek"] = False
    return state


# Guess evaluation
def evaluate_guess(
    state: dict[str, Any],
    player_index: int,
    lat: float | None,
    lng: float | None,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Evaluate a player's guess and append it to round_guesses."""
    player = state["players"][player_index]
    question = state["question"]

    progress_map = state.get("detective_progress") or {}
    detective_progress = (
        progress_map.get(player_index) if isinstance(progress_map, dict) else progress_map
    ) or _fresh_detective_progress()

    # Timeout / no guess
    if timed_out or lat is None or lng is None:
        ocean = random.choice(OCEAN_POINTS)
        score = -TIMEOUT_PENALTY_PTS

        guess = {
            "player_index": player_index,
            "player_name": player["name"],
            "player_color": player["color"],
            "lat": None,
            "lng": None,
            "phantom_lat": ocean["lat"],
            "phantom_lng": ocean["lng"],
            "distance_km": None,
            "round_score": score,
            "inside_target": False,
            "joker_double": False,
            "detective_clues_used": detective_progress["revealed_clues"],
            "detective_multiplier": detective_progress["score_multiplier"],
            "timed_out": True,
            "guessed_country": None,
        }
        # Discard any pending joker flag so it doesn't carry into the next round
        player.pop("joker_double_active", None)
        # Break streak on timeout
        _update_streak(state, player_index, km=None, timed_out=True)

    # Valid guess
    else:
        # Edge case: coordinates out of valid range
        lat = max(-90.0, min(90.0, lat))
        lng = max(-180.0, min(180.0, lng))

        # Use real country polygon when available, radius as fallback
        if question["mode"].startswith("country_") and question.get("answer_iso"):
            dist_km, inside = _distance_to_country(lat, lng, question["answer_iso"])

        if not question["mode"].startswith("country_") or dist_km is None:
            dist_km, inside = distance_to_zone(
                lat=lat, lng=lng,
                answer_lat=question["answer_lat"],
                answer_lng=question["answer_lng"],
                radius_km=question["radius_km"],
            )

        base_score = 5_000 if inside else score_from_distance(dist_km)
        detective_multiplier = (
            detective_progress["score_multiplier"]
            if question["mode"] == "detective_city"
            else 1
        )
        base_score = round(base_score * detective_multiplier)

        # ×2 joker
        double_used = player.pop("joker_double_active", False)
        final_score = base_score * 2 if double_used else base_score
        guessed_country = _country_at_point(lat, lng)

        # Track best distance
        if player["best_round_km"] is None or dist_km < player["best_round_km"]:
            player["best_round_km"] = dist_km

        guess = {
            "player_index": player_index,
            "player_name": player["name"],
            "player_color": player["color"],
            "lat": lat,
            "lng": lng,
            "phantom_lat": None,
            "phantom_lng": None,
            "distance_km": dist_km,
            "round_score": final_score,
            "inside_target": inside,
            "joker_double": double_used,
            "detective_clues_used": detective_progress["revealed_clues"],
            "detective_multiplier": detective_multiplier,
            "timed_out": False,
            "guessed_country": guessed_country,
        }

        # Perfect guess event
        if inside:
            state["perfect_event"] = {
                "player_name": player["name"],
                "player_color": player["color"],
                "player_index": player_index,
            }

        _update_streak(state, player_index, km=dist_km, timed_out=False)

    state["round_guesses"].append(guess)
    return state


def _update_streak(
    state: dict[str, Any],
    player_index: int,
    km: int | None,
    timed_out: bool,
) -> None:
    """Update streak counter and award bonus if threshold reached."""
    p = state["players"][player_index]

    if timed_out or km is None or km >= STREAK_THRESHOLD_KM:
        p["streak"] = 0
        return

    p["streak"] = p.get("streak", 0) + 1

    if p["streak"] >= STREAK_REQUIRED:
        # Award bonus every round once streak is active
        p["total_score"] = p.get("total_score", 0) + STREAK_BONUS_PTS
        p["streak_bonus_earned"] = p.get("streak_bonus_earned", 0) + STREAK_BONUS_PTS
        state["streak_event"] = {
            "player_name": p["name"],
            "player_color": p["color"],
            "bonus": STREAK_BONUS_PTS,
        }


# Turn / phase transitions
def next_player_or_reveal(state: dict[str, Any]) -> dict[str, Any]:
    """Move to results once every active (non-disconnected) player has guessed."""
    active_indices = {
        i for i, p in enumerate(state["players"]) if not p.get("disconnected")
    }
    guessed_indices = {g["player_index"] for g in state["round_guesses"]}
    if not active_indices.issubset(guessed_indices):
        return state  # still waiting for active players

    # All guesses in — apply scores
    for guess in state["round_guesses"]:
        p = state["players"][guess["player_index"]]
        p["total_score"] = max(0, p.get("total_score", 0) + guess["round_score"])
        guess["total_after_round"] = p["total_score"]

    valid = [g for g in state["round_guesses"] if not g["timed_out"]]
    if valid:
        winner = min(valid, key=lambda g: g["distance_km"])
        state["round_winner_index"] = winner["player_index"]
    else:
        state["round_winner_index"] = None

    state["phase"] = "results"
    state["detective_progress"] = None
    return state


# Public state (safe to send to client)
def _public_question(
    question: dict[str, Any],
    *,
    include_answer: bool,
    detective_clues: int = 1,
) -> dict[str, Any]:
    """Return a browser-safe question payload for the current phase."""
    mode = question["mode"]
    item = question["item"]

    if include_answer:
        return deepcopy(question)

    public_item: dict[str, Any]
    if mode == "country_flag":
        public_item = {"iso": item["iso"]}
    elif mode == "country_capital":
        public_item = {"capital": item["capital"]}
    elif mode == "country_stats":
        public_item = {"clues": list(item["clues"])}
    elif mode == "country_landmark":
        public_item = {"landmark": item["landmark"]}
    elif mode == "city_name":
        public_item = {"name": item["name"]}
    elif mode == "city_facts":
        public_item = {"facts": list(item["facts"])}
    elif mode == "detective_city":
        public_item = {
            "detective_clues": list(item["detective_clues"][: max(1, detective_clues)]),
        }
    else:
        public_item = {}

    return {
        "mode": mode,
        "item": public_item,
        "kind": question["kind"],
    }


def get_public_state(state: dict[str, Any], viewer_index: int | None = None) -> dict[str, Any]:
    """Return a copy of state safe to expose to the browser.

    During guessing phase, hide other players' guess locations and scores.
    """
    public = deepcopy(state)
    public.pop("pending_guesses", None)

    # detective_progress is now a dict[player_index → progress] — expose only the viewer's own
    if isinstance(public.get("detective_progress"), dict):
        public["detective_progress"] = (
            public["detective_progress"].get(viewer_index) if viewer_index is not None else None
        )

    if public.get("question"):
        if public["phase"] == "guessing":
            revealed = (public.get("detective_progress") or {}).get("revealed_clues", 1)
            public["question"] = _public_question(
                public["question"],
                include_answer=False,
                detective_clues=revealed,
            )
        else:
            public["question"] = _public_question(public["question"], include_answer=True)

    if public["phase"] == "guessing":
        # Hide already-submitted guesses so other players can't peek at them
        for guess in public["round_guesses"]:
            guess["distance_km"] = None
            guess["round_score"] = None
            guess["lat"] = None
            guess["lng"] = None
            guess["inside_target"] = None
            guess["phantom_lat"] = None
            guess["phantom_lng"] = None
            guess["guessed_country"] = None

    # Clear one-shot events after reading
    streak_event = public.pop("streak_event", None)
    perfect_event = public.pop("perfect_event", None)

    # Keep events in real state until we've sent them once
    # (they are cleared on next state mutation)
    public["streak_event"] = streak_event
    public["perfect_event"] = perfect_event

    return public
