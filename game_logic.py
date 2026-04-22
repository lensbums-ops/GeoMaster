"""GeoMaster — complete game logic in Python.

All scoring, streaks, jokers, phantom guesses, category selection,
and state transitions live here. The frontend only handles display.
"""
from __future__ import annotations

import math
import random
from copy import deepcopy
from typing import Any

from data import (
    CITIES,
    COUNTRIES,
    DETECTIVE_CITIES,
    OCEAN_POINTS,
    PLAYER_COLORS,
    QUESTION_MODES,
)

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_DISTANCE_KM = 10_000   # 0 km = 5000 pts, 10000 km = 0 pts (linear)
STREAK_THRESHOLD_KM = 1_000  # under this = good round for streak
STREAK_REQUIRED = 3          # rounds in a row needed to earn bonus
STREAK_BONUS_PTS = 500
TIMEOUT_PENALTY_PTS = 500
DETECTIVE_MAX_CLUES = 3
DETECTIVE_MULTIPLIERS = {1: 3, 2: 2, 3: 1}


# ─── Maths helpers ────────────────────────────────────────────────────────────
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


# ─── Game state creation ───────────────────────────────────────────────────────
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
        "current_turn_position": 0,
        "phase": "setup",
        "question": None,
        "round_guesses": [],
        "round_turn_order": [],
        "streak_event": None,
        "perfect_event": None,
        # Shuffled index pools — pop from front each round, no repeats
        "country_pool": country_pool,
        "city_pool": city_pool,
        "detective_pool": detective_pool,
        "detective_progress": None,
    }


# ─── Question generation ──────────────────────────────────────────────────────
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
    }


def _fresh_detective_progress() -> dict[str, int]:
    """Return the default detective clue state for a new player's turn."""
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


def _build_round_turn_order(state: dict[str, Any]) -> list[int]:
    """Rotate the guess order so a different player starts each round."""
    player_count = len(state.get("players", []))
    if player_count == 0:
        return []

    start_index = get_category_picker_index(state) or 0
    return list(range(start_index, player_count)) + list(range(0, start_index))


# ─── Round management ─────────────────────────────────────────────────────────
def start_round(state: dict[str, Any], chosen_mode: str) -> dict[str, Any]:
    """Begin a round with the given mode (chosen by a player)."""
    round_turn_order = _build_round_turn_order(state)
    state["round_turn_order"] = round_turn_order
    state["current_turn_position"] = 0
    state["current_player_index"] = round_turn_order[0] if round_turn_order else 0
    state["question"] = build_question(chosen_mode, state)
    state["round_guesses"] = []
    state["streak_event"] = None
    state["perfect_event"] = None
    state["phase"] = "guessing"
    state["detective_progress"] = (
        _fresh_detective_progress() if chosen_mode == "detective_city" else None
    )
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
    state["streak_event"] = None
    state["perfect_event"] = None
    state["detective_progress"] = None
    state["round_turn_order"] = []
    state["current_turn_position"] = 0
    return state


def reveal_detective_hint(state: dict[str, Any]) -> dict[str, Any]:
    """Reveal one more detective clue for the current player."""
    if state["phase"] != "guessing":
        return state

    question = state.get("question") or {}
    if question.get("mode") != "detective_city":
        return state

    progress = state.get("detective_progress") or _fresh_detective_progress()
    revealed = min(DETECTIVE_MAX_CLUES, progress["revealed_clues"] + 1)
    progress["revealed_clues"] = revealed
    progress["score_multiplier"] = DETECTIVE_MULTIPLIERS[revealed]
    state["detective_progress"] = progress
    return state


# ─── Jokers ───────────────────────────────────────────────────────────────────
def use_joker_double(state: dict[str, Any]) -> dict[str, Any]:
    """Activate the ×2 joker for the current player."""
    p = state["players"][state["current_player_index"]]

    # Edge case: joker already used
    if not p["joker_double"]:
        return state  # silently ignore

    # Edge case: wrong phase
    if state["phase"] != "guessing":
        return state

    p["joker_double"] = False
    p["joker_double_active"] = True   # flag consumed in evaluate_guess
    return state


def use_joker_peek(state: dict[str, Any]) -> dict[str, Any]:
    """Mark the peek joker as used for the current player.

    The actual 3-second outline display is handled by the frontend;
    Python just records that the joker was spent.
    """
    p = state["players"][state["current_player_index"]]

    # Edge case: joker already used
    if not p.get("joker_peek", False):
        return state

    if state["phase"] != "guessing":
        return state

    p["joker_peek"] = False
    return state


# ─── Guess evaluation ─────────────────────────────────────────────────────────
def evaluate_guess(
    state: dict[str, Any],
    lat: float | None,
    lng: float | None,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Evaluate a player's guess and append it to round_guesses."""
    player_index = state["current_player_index"]
    player = state["players"][player_index]
    question = state["question"]

    # ── Timeout / no guess ──────────────────────────────────────────────────
    if timed_out or lat is None or lng is None:
        ocean = random.choice(OCEAN_POINTS)
        score = -TIMEOUT_PENALTY_PTS
        detective_progress = state.get("detective_progress") or _fresh_detective_progress()

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
        }
        # Break streak on timeout
        _update_streak(state, player_index, km=None, timed_out=True)

    # ── Valid guess ──────────────────────────────────────────────────────────
    else:
        # Edge case: coordinates out of valid range
        lat = max(-90.0, min(90.0, lat))
        lng = max(-180.0, min(180.0, lng))

        dist_km, inside = distance_to_zone(
            lat=lat, lng=lng,
            answer_lat=question["answer_lat"],
            answer_lng=question["answer_lng"],
            radius_km=question["radius_km"],
        )

        base_score = 5_000 if inside else score_from_distance(dist_km)
        detective_progress = state.get("detective_progress") or _fresh_detective_progress()
        detective_multiplier = (
            detective_progress["score_multiplier"]
            if question["mode"] == "detective_city"
            else 1
        )
        base_score *= detective_multiplier

        # ×2 joker
        double_used = player.pop("joker_double_active", False)
        final_score = base_score * 2 if double_used else base_score

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
        }

        # Perfect guess event
        if inside:
            state["perfect_event"] = {
                "player_name": player["name"],
                "player_color": player["color"],
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


# ─── Turn / phase transitions ─────────────────────────────────────────────────
def next_player_or_reveal(state: dict[str, Any]) -> dict[str, Any]:
    """Advance to next player, or to results phase if all have guessed."""
    state["current_turn_position"] = state.get("current_turn_position", 0) + 1
    round_turn_order = state.get("round_turn_order") or list(range(len(state["players"])))

    # Edge case: more players remaining
    if state["current_turn_position"] < len(round_turn_order):
        state["current_player_index"] = round_turn_order[state["current_turn_position"]]
        state["phase"] = "guessing"
        state["detective_progress"] = (
            _fresh_detective_progress()
            if state.get("question", {}).get("mode") == "detective_city"
            else None
        )
        return state

    # All players done — apply scores and switch to results
    for guess in state["round_guesses"]:
        p = state["players"][guess["player_index"]]
        # Edge case: total_score cannot go below 0
        p["total_score"] = max(0, p.get("total_score", 0) + guess["round_score"])
        guess["total_after_round"] = p["total_score"]

    # Find round winner (smallest distance among non-timeout guesses)
    valid = [g for g in state["round_guesses"] if not g["timed_out"]]
    if valid:
        winner = min(valid, key=lambda g: g["distance_km"])
        state["round_winner_index"] = winner["player_index"]
    else:
        state["round_winner_index"] = None

    state["phase"] = "results"
    state["detective_progress"] = None
    return state


# ─── Public state (safe to send to client) ────────────────────────────────────
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


def get_public_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of state safe to expose to the browser.

    During guessing phase, hide other players' guess locations and scores.
    """
    public = deepcopy(state)

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

    # Clear one-shot events after reading
    streak_event = public.pop("streak_event", None)
    perfect_event = public.pop("perfect_event", None)

    # Keep events in real state until we've sent them once
    # (they are cleared on next state mutation)
    public["streak_event"] = streak_event
    public["perfect_event"] = perfect_event

    return public
