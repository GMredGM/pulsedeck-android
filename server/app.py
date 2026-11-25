"""
PulseDeck - Helper Server
--------------------------------
Runs on your PC. Exposes a small local-network API that the Android app
polls/calls to:
  - show + control what's playing on Spotify
  - show your Steam online/away/in-game status
  - time how long a game has currently been running (live session timer)

SETUP (one-time):
  1. pip install -r requirements.txt
  2. Create a Spotify app at https://developer.spotify.com/dashboard
     - Add redirect URI: http://127.0.0.1:8888/callback
     - Copy the Client ID and Client Secret into config.json (see config.example.json)
  3. Get a Steam Web API key: https://steamcommunity.com/dev/apikey
     - Get your 64-bit SteamID (e.g. via https://steamid.io)
     - Put both into config.json
  4. In config.json, list the game processes you want tracked, e.g.:
       "tracked_processes": {
           "Hollow Knight": "hollow_knight.exe",
           "Deep Rock Galactic": "FSD-Win64-Shipping.exe"
       }
  5. Run: python app.py
  6. Note the IP address it prints (e.g. 192.168.1.42) and the AUTH_TOKEN
     it generates on first run (saved in config.json) -- you'll enter both
     into the Android app's settings screen.

This server is meant for your home network only. It checks a simple shared
token on every request; it is NOT meant to be exposed to the public internet.
"""

import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path

import psutil
import spotipy
from flask import Flask, jsonify, request
from spotipy.oauth2 import SpotifyOAuth

try:
    import requests
except ImportError:
    requests = None

# When packaged as an .exe, keep config next to the .exe (not in PyInstaller's temp dir)
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"


def pause_if_exe():
    """Keep the console window open in the .exe so messages can be read."""
    if FROZEN:
        try:
            input("\nPress Enter to close...")
        except Exception:
            pass

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config load / first-run setup
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_redirect_uri": "http://127.0.0.1:8888/callback",
    "steam_api_key": "",
    "steam_id": "",
    "tracked_processes": {},
    "auth_token": "",
    "port": 5000,
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(f"Created {CONFIG_PATH} - fill it in, then re-run this program.")
        pause_if_exe()
        raise SystemExit(0)
    cfg = json.loads(CONFIG_PATH.read_text())
    changed = False
    if not cfg.get("auth_token"):
        cfg["auth_token"] = secrets.token_hex(16)
        changed = True
    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg


CONFIG = load_config()

# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


@app.before_request
def check_auth():
    token = request.headers.get("X-Auth-Token")
    if token != CONFIG["auth_token"]:
        return jsonify({"error": "unauthorized"}), 401


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

_sp_client = None


def get_spotify():
    global _sp_client
    if _sp_client is None:
        auth = SpotifyOAuth(
            client_id=CONFIG["spotify_client_id"],
            client_secret=CONFIG["spotify_client_secret"],
            redirect_uri=CONFIG["spotify_redirect_uri"],
            scope="user-read-playback-state user-modify-playback-state user-read-currently-playing",
            open_browser=True,
            cache_path=str(BASE_DIR / ".spotify_cache"),
        )
        _sp_client = spotipy.Spotify(auth_manager=auth)
    return _sp_client


@app.route("/spotify/now")
def spotify_now():
    sp = get_spotify()
    current = sp.current_playback()
    if not current or not current.get("item"):
        return jsonify({"playing": False})
    item = current["item"]
    return jsonify(
        {
            "playing": current.get("is_playing", False),
            "track": item.get("name"),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
            "album": item.get("album", {}).get("name"),
            "progress_ms": current.get("progress_ms"),
            "duration_ms": item.get("duration_ms"),
            "volume_percent": current.get("device", {}).get("volume_percent"),
        }
    )


@app.route("/spotify/play", methods=["POST"])
def spotify_play():
    get_spotify().start_playback()
    return jsonify({"ok": True})


@app.route("/spotify/pause", methods=["POST"])
def spotify_pause():
    get_spotify().pause_playback()
    return jsonify({"ok": True})


@app.route("/spotify/next", methods=["POST"])
def spotify_next():
    get_spotify().next_track()
    return jsonify({"ok": True})


@app.route("/spotify/previous", methods=["POST"])
def spotify_previous():
    get_spotify().previous_track()
    return jsonify({"ok": True})


@app.route("/spotify/volume", methods=["POST"])
def spotify_volume():
    pct = int(request.json.get("percent", 50))
    get_spotify().volume(max(0, min(100, pct)))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Steam status
# ---------------------------------------------------------------------------

STEAM_STATE_MAP = {
    0: "Offline",
    1: "Online",
    2: "Busy",
    3: "Away",
    4: "Snooze",
    5: "Looking to trade",
    6: "Looking to play",
}


@app.route("/steam/status")
def steam_status():
    if requests is None:
        return jsonify({"error": "requests library not installed"}), 500
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
    params = {"key": CONFIG["steam_api_key"], "steamids": CONFIG["steam_id"]}
    r = requests.get(url, params=params, timeout=5)
    r.raise_for_status()
    players = r.json().get("response", {}).get("players", [])
    if not players:
        return jsonify({"error": "no player found - check steam_id"}), 404
    p = players[0]
    return jsonify(
        {
            "persona_name": p.get("personaname"),
            "status": STEAM_STATE_MAP.get(p.get("personastate"), "Unknown"),
            "in_game": p.get("gameextrainfo"),  # name of game if currently playing
        }
    )


# ---------------------------------------------------------------------------
# Live session timer (local process watch)
# ---------------------------------------------------------------------------

# Tracks {game_name: start_timestamp} for processes currently detected running
_running_since = {}


def scan_tracked_processes():
    tracked = CONFIG.get("tracked_processes", {})
    if not tracked:
        return
    running_exes = {p.name().lower() for p in psutil.process_iter(["name"])}
    for game_name, exe_name in tracked.items():
        is_running = exe_name.lower() in running_exes
        if is_running and game_name not in _running_since:
            _running_since[game_name] = time.time()
        elif not is_running and game_name in _running_since:
            del _running_since[game_name]


@app.route("/playtime/live")
def playtime_live():
    scan_tracked_processes()
    now = time.time()
    sessions = [
        {
            "game": game,
            "seconds": int(now - started),
        }
        for game, started in _running_since.items()
    ]
    return jsonify({"sessions": sessions})


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "message": "PulseDeck server is up"})


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    print("=" * 60)
    print("PulseDeck - helper server")
    print(f"  Local IP for the Android app:  {local_ip()}")
    print(f"  Port:                          {CONFIG['port']}")
    print(f"  Auth token (enter in app):     {CONFIG['auth_token']}")
    print("=" * 60)
    try:
        app.run(host="0.0.0.0", port=CONFIG["port"])
    except Exception as e:
        print(f"Server stopped: {e}")
        pause_if_exe()

