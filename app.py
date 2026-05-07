"""GeoMaster — Flask application entry point with room-based multiplayer."""
from __future__ import annotations

import os
import random
import time
from typing import Any
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, session

from data import PLAYER_COLORS, QUESTION_MODES
from game_logic import (
    begin_next_round,
    create_game_state,
    evaluate_guess,
    get_category_picker_index,
    get_public_state,
    next_player_or_reveal,
    reveal_detective_hint,
    start_round,
    use_joker_double,
    use_joker_peek,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

ROOMS: dict[str, dict[str, Any]] = {}
ROUND_TIME_SECONDS = 30
RESULTS_AUTO_ADVANCE_SECONDS = 15
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_TTL_MS = 5 * 60 * 1000  # delete rooms inactive for 5 minutes
WORLD_BORDERS_GEOJSON: dict[str, Any] | None = None


# Time / room helpers
def _now_ms() -> int:
    return int(time.time() * 1000)


def _generate_room_code() -> str:
    while True:
        code = "".join(random.choices(ROOM_CODE_ALPHABET, k=5))
        if code not in ROOMS:
            return code


def _ensure_player_id() -> str:
    player_id = session.get("player_id")
    if not player_id:
        player_id = uuid4().hex
        session["player_id"] = player_id
    return player_id


def _clear_room_session() -> None:
    session.pop("room_code", None)


def _get_room() -> dict[str, Any] | None:
    room_code = session.get("room_code")
    if not room_code:
        return None

    room = ROOMS.get(room_code)
    if room is None:
        _clear_room_session()
        return None
    return room


def _find_player_index(room: dict[str, Any], player_id: str) -> int | None:
    for index, player in enumerate(room["players"]):
        if player["id"] == player_id:
            return index
    return None


def _player_in_room(room: dict[str, Any], player_id: str) -> bool:
    return _find_player_index(room, player_id) is not None


def _touch_room(room: dict[str, Any]) -> None:
    room["updated_at_ms"] = _now_ms()
    room["version"] = room.get("version", 0) + 1


def _touch_game_state(state: dict[str, Any]) -> None:
    state["version"] = state.get("version", 0) + 1
    state["updated_at_ms"] = _now_ms()


def _set_turn_deadline(state: dict[str, Any], *, reset: bool) -> None:
    if state.get("phase") == "guessing":
        if reset or not state.get("turn_ends_at_ms"):
            state["turn_ends_at_ms"] = _now_ms() + ROUND_TIME_SECONDS * 1000
    else:
        state["turn_ends_at_ms"] = None


def _set_results_deadline(state: dict[str, Any]) -> None:
    if state.get("phase") == "results" and not state.get("results_ends_at_ms"):
        state["results_ends_at_ms"] = _now_ms() + RESULTS_AUTO_ADVANCE_SECONDS * 1000


def _clear_transient_events(state: dict[str, Any]) -> None:
    state["streak_event"] = None
    state["perfect_event"] = None


def _parse_coordinates(payload: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    try:
        lat = float(payload["lat"])
        lng = float(payload["lng"])
    except (KeyError, ValueError, TypeError):
        return None, None, "Invalid coordinates — provide lat and lng as numbers"

    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return None, None, "Coordinates out of valid range"

    return lat, lng, None


def _is_stale_round_payload(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    if "round" not in payload:
        return False
    try:
        return int(payload["round"]) != int(state.get("current_round", 0))
    except (ValueError, TypeError):
        return True


def _pending_guess_for(state: dict[str, Any], player_index: int) -> dict[str, Any] | None:
    pending = state.get("pending_guesses") or {}
    guess = pending.get(player_index) or pending.get(str(player_index))
    if not isinstance(guess, dict):
        return None
    try:
        return {"lat": float(guess["lat"]), "lng": float(guess["lng"])}
    except (KeyError, ValueError, TypeError):
        return None


def _clear_pending_guess(state: dict[str, Any], player_index: int) -> None:
    pending = state.get("pending_guesses")
    if isinstance(pending, dict):
        pending.pop(player_index, None)
        pending.pop(str(player_index), None)


def _evaluate_timeout_or_pending(state: dict[str, Any], player_index: int) -> dict[str, Any]:
    pending = _pending_guess_for(state, player_index)
    if pending:
        state = evaluate_guess(
            state,
            player_index=player_index,
            lat=pending["lat"],
            lng=pending["lng"],
        )
    else:
        state = evaluate_guess(state, player_index=player_index, lat=None, lng=None, timed_out=True)
    _clear_pending_guess(state, player_index)
    return state


def _cleanup_stale_rooms() -> None:
    cutoff = _now_ms() - ROOM_TTL_MS
    stale = [code for code, room in list(ROOMS.items()) if room.get("updated_at_ms", 0) < cutoff]
    for code in stale:
        ROOMS.pop(code, None)


def _sync_room_timeouts(room: dict[str, Any]) -> None:
    state = room.get("game")
    if not state:
        return

    # On finished screen: mark players as disconnected if they haven't polled in 10s
    if state.get("phase") == "finished":
        cutoff = _now_ms() - 10_000
        changed = False
        for i, rp in enumerate(room["players"]):
            if not rp.get("disconnected") and rp.get("last_seen_ms", 0) < cutoff:
                rp["disconnected"] = True
                if i < len(state["players"]):
                    state["players"][i]["disconnected"] = True
                changed = True
        if changed:
            _touch_room(room)
        return

    # Auto-advance from results if host hasn't clicked within 15 seconds
    if state.get("phase") == "results":
        _set_results_deadline(state)
        if _now_ms() >= state.get("results_ends_at_ms", 0):
            state["results_ends_at_ms"] = None
            _clear_transient_events(state)
            state = begin_next_round(state)
            _set_turn_deadline(state, reset=False)
            _touch_game_state(state)
            room["game"] = state
            _touch_room(room)
        return

    if state.get("phase") != "guessing":
        return

    guessed = {g["player_index"] for g in state.get("round_guesses", [])}

    # Immediately phantom any disconnected players who haven't guessed yet
    disconnected_unguessed = [
        i for i, p in enumerate(state["players"])
        if p.get("disconnected") and i not in guessed
    ]
    if disconnected_unguessed:
        _clear_transient_events(state)
        for pi in disconnected_unguessed:
            state = _evaluate_timeout_or_pending(state, pi)
        state = next_player_or_reveal(state)
        _set_turn_deadline(state, reset=False)
        _touch_game_state(state)
        room["game"] = state
        _touch_room(room)
        if state.get("phase") != "guessing":
            return
        guessed = {g["player_index"] for g in state.get("round_guesses", [])}

    # Phantom remaining players once the timer expires
    deadline = state.get("turn_ends_at_ms")
    if deadline is None or _now_ms() < deadline:
        return

    unguessed = [
        i for i, p in enumerate(state["players"])
        if i not in guessed and not p.get("disconnected")
    ]
    if not unguessed:
        return

    _clear_transient_events(state)
    for pi in unguessed:
        state = _evaluate_timeout_or_pending(state, pi)

    state = next_player_or_reveal(state)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)


def _remove_player_from_room(room: dict[str, Any], player_id: str) -> None:
    index = _find_player_index(room, player_id)
    if index is None:
        return

    room["players"].pop(index)
    if not room["players"]:
        ROOMS.pop(room["code"], None)
        return

    if room["host_id"] == player_id:
        room["host_id"] = room["players"][0]["id"]

    _touch_room(room)


def _room_payload(room: dict[str, Any]) -> dict[str, Any]:
    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    viewer = room["players"][viewer_index] if viewer_index is not None else None

    votes = room.get("rematch_votes", [])
    active_players = [p for p in room["players"] if not p.get("disconnected")]
    return {
        "room_code": room["code"],
        "started": room["started"],
        "rounds": room["rounds"],
        "is_host": player_id == room["host_id"],
        "viewer_index": viewer_index,
        "viewer_name": viewer["name"] if viewer else None,
        "host_name": next((p["name"] for p in room["players"] if p["id"] == room["host_id"]), None),
        "players": [
            {
                "name": player["name"],
                "color": player["color"],
                "disconnected": player.get("disconnected", False),
            }
            for player in room["players"]
        ],
        "version": room.get("version", 0),
        "rematch_votes": len(votes),
        "rematch_total": len(active_players),
        "viewer_voted_rematch": player_id in votes,
    }


def _game_payload(room: dict[str, Any]) -> dict[str, Any] | None:
    state = room.get("game")
    if not state:
        return None

    _sync_room_timeouts(room)
    state = room.get("game")
    if not state:
        return None

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    picker_index = get_category_picker_index(state)
    guessed_indices = {g["player_index"] for g in state.get("round_guesses", [])}
    public = get_public_state(state, viewer_index=viewer_index)
    public.update(
        {
            "room_code": room["code"],
            "viewer_index": viewer_index,
            "viewer_name": room["players"][viewer_index]["name"] if viewer_index is not None else None,
            "is_host": player_id == room["host_id"],
            "can_choose_category": state["phase"] == "category_pick" and viewer_index == picker_index,
            "can_guess": state["phase"] == "guessing" and viewer_index is not None and viewer_index not in guessed_indices,
            "can_advance_round": state["phase"] == "results" and player_id == room["host_id"],
            "current_picker_index": picker_index,
            "turn_seconds": ROUND_TIME_SECONDS,
            "server_now_ms": _now_ms(),
            "room_players": [
                {"name": player["name"], "color": player["color"]}
                for player in room["players"]
            ],
        }
    )
    return public


def _payload(room: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload = {
        "room": _room_payload(room),
        "game": _game_payload(room) if room["started"] else None,
    }
    payload.update(extra)
    return payload


def _ok(room: dict[str, Any], **extra: Any) -> tuple:
    return jsonify(_payload(room, **extra)), 200


def _err(msg: str, code: int = 400) -> tuple:
    return jsonify({"error": msg}), code


def _current_game_room() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    room = _get_room()
    if not room:
        return None, None

    _sync_room_timeouts(room)
    state = room.get("game")
    if not room["started"] or not state:
        return room, None
    return room, state


# Pages
@app.get("/")
def index():
    return render_template("index.html")


# Room lifecycle
@app.get("/api/bootstrap")
def bootstrap():
    room = _get_room()
    if not room:
        return jsonify({"room": None, "game": None}), 200
    return jsonify(_payload(room)), 200


@app.get("/api/state")
def get_state():
    _cleanup_stale_rooms()
    room = _get_room()
    if not room:
        return jsonify({"room": None, "game": None}), 200
    # Track when this player was last seen (used for AFK detection on final screen)
    player_id = _ensure_player_id()
    idx = _find_player_index(room, player_id)
    if idx is not None:
        room["players"][idx]["last_seen_ms"] = _now_ms()
    return jsonify(_payload(room)), 200


@app.post("/api/room/create")
def create_room():
    player_id = _ensure_player_id()
    current_room = _get_room()
    if current_room and not current_room["started"]:
        _remove_player_from_room(current_room, player_id)
        _clear_room_session()

    payload = request.get_json(force=True)
    name = str(payload.get("name", "")).strip()
    if not name:
        return _err("Please enter your player name")

    code = _generate_room_code()
    room = {
        "code": code,
        "host_id": player_id,
        "players": [
            {
                "id": player_id,
                "name": name,
                "color": PLAYER_COLORS[0],
            }
        ],
        "started": False,
        "rounds": 5,
        "game": None,
        "created_at_ms": _now_ms(),
        "updated_at_ms": _now_ms(),
        "version": 1,
    }
    ROOMS[code] = room
    session["room_code"] = code
    return _ok(room)


@app.post("/api/room/join")
def join_room():
    payload = request.get_json(force=True)
    code = str(payload.get("room_code", "")).strip().upper()
    name = str(payload.get("name", "")).strip()

    if not code:
        return _err("Please enter a room code")
    if not name:
        return _err("Please enter your player name")

    room = ROOMS.get(code)
    if room is None:
        return _err("Room not found", 404)
    if room["started"]:
        return _err("This room has already started")
    if len(room["players"]) >= 4:
        return _err("This room is already full")
    if any(player["name"].lower() == name.lower() for player in room["players"]):
        return _err("That player name is already taken in this room")

    player_id = _ensure_player_id()
    current_room = _get_room()
    if current_room and current_room["code"] != room["code"] and not current_room["started"]:
        _remove_player_from_room(current_room, player_id)
        _clear_room_session()

    if _player_in_room(room, player_id):
        session["room_code"] = room["code"]
        return _ok(room)

    room["players"].append(
        {
            "id": player_id,
            "name": name,
            "color": PLAYER_COLORS[len(room["players"])],
        }
    )
    _touch_room(room)
    session["room_code"] = room["code"]
    return _ok(room)


@app.post("/api/room/leave")
def leave_room():
    room = _get_room()
    if not room:
        return jsonify({"room": None, "game": None}), 200

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    state = room.get("game")

    # Mid-game leave: mark disconnected and phantom if still guessing
    if room["started"] and state and state.get("phase") not in ("finished",) and viewer_index is not None:
        # Mark disconnected in room players list and game state
        room["players"][viewer_index]["disconnected"] = True
        state["players"][viewer_index]["disconnected"] = True

        # Transfer host if needed
        if room["host_id"] == player_id:
            for p in room["players"]:
                if p["id"] != player_id and not p.get("disconnected"):
                    room["host_id"] = p["id"]
                    break

        # Auto-phantom if they haven't guessed yet
        if state["phase"] == "guessing":
            guessed = {g["player_index"] for g in state.get("round_guesses", [])}
            if viewer_index not in guessed:
                _clear_transient_events(state)
                state = evaluate_guess(state, player_index=viewer_index, lat=None, lng=None, timed_out=True)
                state = next_player_or_reveal(state)
                _set_turn_deadline(state, reset=False)
                _touch_game_state(state)
                room["game"] = state

        # If everyone left, delete the room
        active = [p for p in room["players"] if not p.get("disconnected")]
        if not active:
            ROOMS.pop(room["code"], None)
            _clear_room_session()
            return jsonify({"room": None, "game": None}), 200

        _touch_room(room)
        _clear_room_session()
        return jsonify({"room": None, "game": None}), 200

    # Lobby leave (game not started or finished)
    _remove_player_from_room(room, player_id)
    _clear_room_session()
    return jsonify({"room": None, "game": None}), 200


# Game lifecycle
@app.post("/api/start")
def start_game():
    """Host starts a room game and jumps straight to category_pick for round 1."""
    room = _get_room()
    if not room:
        return _err("Join or create a room first", 404)

    player_id = _ensure_player_id()
    if room["host_id"] != player_id:
        return _err("Only the host can start the game", 403)
    if room["started"]:
        return _err("Game already started")

    payload = request.get_json(force=True)
    rounds = int(payload.get("rounds", 5))
    player_names = [player["name"] for player in room["players"]]

    try:
        state = create_game_state(players=player_names, rounds=rounds)
    except ValueError as exc:
        return _err(str(exc))

    state = begin_next_round(state)
    _set_turn_deadline(state, reset=False)
    _touch_game_state(state)

    room["game"] = state
    room["started"] = True
    room["rounds"] = state["rounds"]
    _touch_room(room)
    return _ok(room)


@app.post("/api/category")
def choose_category():
    """Current picker chooses a category mode — category_pick → guessing."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    if state["phase"] != "category_pick":
        return _err("Not in category pick phase")

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    if viewer_index != get_category_picker_index(state):
        return _err("Only the round picker can choose the category", 403)

    payload = request.get_json(force=True)
    mode = payload.get("mode", "")
    if mode not in QUESTION_MODES:
        return _err(f"Invalid mode '{mode}'")

    _clear_transient_events(state)
    state = start_round(state, chosen_mode=mode)
    _set_turn_deadline(state, reset=True)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(room)


@app.post("/api/guess")
def submit_guess():
    """Submit a lat/lng guess for the current player."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    if state["phase"] != "guessing":
        return _err("Not in guessing phase")

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    if viewer_index is None:
        return _err("You are not in this room", 403)
    guessed = {g["player_index"] for g in state.get("round_guesses", [])}
    if viewer_index in guessed:
        return _err("You have already guessed this round", 403)

    payload = request.get_json(force=True)
    if _is_stale_round_payload(state, payload):
        return _err("Stale guess placement")
    lat, lng, error = _parse_coordinates(payload)
    if error:
        return _err(error)

    _clear_transient_events(state)
    state = evaluate_guess(state, player_index=viewer_index, lat=lat, lng=lng)
    _clear_pending_guess(state, viewer_index)
    state = next_player_or_reveal(state)
    _set_turn_deadline(state, reset=False)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(room)


@app.post("/api/guess/placement")
def save_guess_placement():
    """Remember the current marker so timeout can auto-confirm it."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    if state["phase"] != "guessing":
        return _err("Not in guessing phase")

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    if viewer_index is None:
        return _err("You are not in this room", 403)
    guessed = {g["player_index"] for g in state.get("round_guesses", [])}
    if viewer_index in guessed:
        return _err("You have already guessed this round", 403)

    payload = request.get_json(force=True)
    if _is_stale_round_payload(state, payload):
        return _err("Stale guess placement")
    lat, lng, error = _parse_coordinates(payload)
    if error:
        return _err(error)

    state.setdefault("pending_guesses", {})[viewer_index] = {"lat": lat, "lng": lng}
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(room)


@app.post("/api/timeout")
def submit_timeout():
    """Timer expired — confirm placed marker or register phantom penalty."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    if state["phase"] != "guessing":
        return _err("Not in guessing phase")

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    if viewer_index is None:
        return _err("You are not in this room", 403)
    guessed = {g["player_index"] for g in state.get("round_guesses", [])}
    if viewer_index in guessed:
        return _err("You have already guessed this round", 403)

    payload = request.get_json(silent=True) or {}
    if "lat" in payload or "lng" in payload:
        if _is_stale_round_payload(state, payload):
            return _err("Stale guess placement")
        lat, lng, error = _parse_coordinates(payload)
        if error:
            return _err(error)
        state.setdefault("pending_guesses", {})[viewer_index] = {"lat": lat, "lng": lng}

    _clear_transient_events(state)
    state = _evaluate_timeout_or_pending(state, viewer_index)
    state = next_player_or_reveal(state)
    _set_turn_deadline(state, reset=False)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(room)


@app.post("/api/detective/hint")
def reveal_hint():
    """Reveal the next detective clue for the current player."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    if state["phase"] != "guessing":
        return _err("Not in guessing phase")

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    if viewer_index is None:
        return _err("You are not in this room", 403)
    guessed = {g["player_index"] for g in state.get("round_guesses", [])}
    if viewer_index in guessed:
        return _err("You have already guessed this round", 403)

    question = state.get("question") or {}
    if question.get("mode") != "detective_city":
        return _err("Detective hints are only available in Detective City mode")

    progress_map = state.get("detective_progress") or {}
    progress = progress_map.get(viewer_index) or {}
    if progress.get("revealed_clues", 1) >= 3:
        return _err("All detective clues are already revealed")

    _clear_transient_events(state)
    state = reveal_detective_hint(state, player_index=viewer_index)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(room)


@app.post("/api/next-round")
def advance_round():
    """Move from results → category_pick for the next round (or finished)."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    player_id = _ensure_player_id()
    if room["host_id"] != player_id:
        return _err("Only the host can advance to the next round", 403)

    if state["phase"] != "results":
        return _err("Not in results phase")

    _clear_transient_events(state)
    state = begin_next_round(state)
    _set_turn_deadline(state, reset=False)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(room)


@app.post("/api/room/rounds")
def set_rounds():
    room = _get_room()
    if not room:
        return _err("No room found", 404)
    if room["started"]:
        return _err("Game already started")

    player_id = _ensure_player_id()
    if room["host_id"] != player_id:
        return _err("Only the host can change the round count", 403)

    payload = request.get_json(force=True)
    try:
        rounds = max(1, min(int(payload.get("rounds", 5)), 30))
    except (ValueError, TypeError):
        return _err("Invalid round count")

    room["rounds"] = rounds
    _touch_room(room)
    return _ok(room)


@app.post("/api/rematch")
def rematch():
    """Cast a rematch vote. Once all players have voted, reset to lobby."""
    room = _get_room()
    if not room:
        return _err("No room found", 404)

    state = room.get("game")
    if not state or state.get("phase") != "finished":
        return _err("Game is not finished yet")

    player_id = _ensure_player_id()
    if not _player_in_room(room, player_id):
        return _err("You are not in this room", 403)

    votes: list[str] = room.setdefault("rematch_votes", [])
    if player_id not in votes:
        votes.append(player_id)
        _touch_room(room)

    active_count = len([p for p in room["players"] if not p.get("disconnected")])
    if len(votes) >= active_count:
        # Keep only active (non-disconnected) players for the rematch
        room["players"] = [p for p in room["players"] if not p.get("disconnected")]
        for i, player in enumerate(room["players"]):
            player["color"] = PLAYER_COLORS[i]
            player["joker_double"] = True
            player["joker_peek"] = True
        room["started"] = False
        room["game"] = None
        room["rematch_votes"] = []
        _touch_room(room)

    return _ok(room)


# Joker endpoints
@app.post("/api/joker/double")
def activate_double():
    """Activate the ×2 joker for the current player."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    if state["phase"] != "guessing":
        return _err("Cannot activate joker now")

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    if viewer_index is None:
        return _err("You are not in this room", 403)
    guessed = {g["player_index"] for g in state.get("round_guesses", [])}
    if viewer_index in guessed:
        return _err("You have already guessed this round", 403)

    player = state["players"][viewer_index]
    if not player.get("joker_double", False):
        return _err("Double joker already used")

    _clear_transient_events(state)
    state = use_joker_double(state, player_index=viewer_index)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(room)


@app.post("/api/joker/peek")
def activate_peek():
    """Mark peek joker as used and return the target outline to the active player."""
    room, state = _current_game_room()
    if not room or not state:
        return _err("No active game", 404)

    if state["phase"] != "guessing":
        return _err("Cannot activate joker now")

    player_id = _ensure_player_id()
    viewer_index = _find_player_index(room, player_id)
    if viewer_index is None:
        return _err("You are not in this room", 403)
    guessed = {g["player_index"] for g in state.get("round_guesses", [])}
    if viewer_index in guessed:
        return _err("You have already guessed this round", 403)

    player = state["players"][viewer_index]
    if not player.get("joker_peek", False):
        return _err("Peek joker already used")

    question = state["question"]
    _clear_transient_events(state)
    state = use_joker_peek(state, player_index=viewer_index)
    _touch_game_state(state)
    room["game"] = state
    _touch_room(room)
    return _ok(
        room,
        peek_target={
            "lat": question["answer_lat"],
            "lng": question["answer_lng"],
            "radius_km": question["radius_km"],
            "iso": question.get("answer_iso"),
            "kind": question.get("kind"),
        },
    )


# Country border GeoJSON
@app.get("/api/borders")
def get_borders():
    """Return all available country borders as a GeoJSON FeatureCollection."""
    global WORLD_BORDERS_GEOJSON

    if WORLD_BORDERS_GEOJSON is None:
        from game_logic import _COUNTRY_BORDERS
        from shapely.geometry import mapping as _mapping

        WORLD_BORDERS_GEOJSON = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"iso": iso},
                    "geometry": _mapping(geom),
                }
                for iso, geom in _COUNTRY_BORDERS.items()
            ],
        }

    return jsonify(WORLD_BORDERS_GEOJSON)


@app.get("/api/border/<iso>")
def get_border(iso: str):
    from game_logic import _COUNTRY_BORDERS
    from shapely.geometry import mapping as _mapping
    geom = _COUNTRY_BORDERS.get(iso.lower())
    if geom is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_mapping(geom))


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
