"""
PulseDeck Server (PC side)
--------------------------
A small desktop app. It runs a tiny web server on your home network that the
PulseDeck Android app talks to, and shows a window with your connection info
(IP, port, pairing code) plus settings for Spotify, Steam and tracked games.

Run from source:   python app.py
Build an .exe:     see .github/workflows/build-exe.yml
"""

import hmac
import json
import logging
import os
import queue
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# A windowed .exe has no console, so stdout/stderr can be None. Fix that
# before importing anything that might print or log.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import psutil
import spotipy
from flask import Flask, jsonify, request
from spotipy.oauth2 import SpotifyOAuth

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logging.getLogger("werkzeug").setLevel(logging.ERROR)

FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I to avoid mix-ups
CODE_LENGTH = 6

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


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception:
            # Hand-edited file with a typo: keep a copy and start fresh
            try:
                CONFIG_PATH.replace(CONFIG_PATH.with_name("config.broken.json"))
            except Exception:
                pass
    # Old long tokens are replaced with the short code
    if not cfg.get("auth_token") or len(cfg["auth_token"]) > 12:
        cfg["auth_token"] = new_code()
    cfg["auth_token"] = cfg["auth_token"].upper()
    if not isinstance(cfg.get("tracked_processes"), dict):
        cfg["tracked_processes"] = {}
    try:
        cfg["port"] = int(cfg.get("port", 5000))
    except Exception:
        cfg["port"] = 5000
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


CONFIG = load_config()


def save_config():
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Auth guard (short code, with a simple brute-force limiter)
# ---------------------------------------------------------------------------

_fails = {}
last_seen = 0.0  # time of the last authorised request (shown in the window)


@app.before_request
def check_auth():
    global last_seen
    ip = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 60]
    _fails[ip] = recent
    if len(recent) >= 10:
        return jsonify({"error": "Too many wrong codes - wait a minute"}), 429
    sent = request.headers.get("X-Auth-Token", "").strip().upper().encode()
    if not hmac.compare_digest(sent, CONFIG["auth_token"].upper().encode()):
        recent.append(now)
        return jsonify({"error": "Wrong code"}), 401
    last_seen = now


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

SPOTIFY_SCOPE = (
    "user-read-playback-state user-modify-playback-state user-read-currently-playing"
)
_auth = None
_auth_key = None
_sp = None
_sp_lock = threading.Lock()


class SpotifyNotReady(Exception):
    pass


def reset_spotify():
    global _auth, _auth_key, _sp
    with _sp_lock:
        _auth = _auth_key = _sp = None


def _get_auth():
    global _auth, _auth_key, _sp
    key = (
        CONFIG["spotify_client_id"],
        CONFIG["spotify_client_secret"],
        CONFIG["spotify_redirect_uri"],
    )
    if not key[0] or not key[1]:
        raise SpotifyNotReady("Add your Spotify keys in the PulseDeck Server window")
    if _auth is None or _auth_key != key:
        _auth = SpotifyOAuth(
            client_id=key[0],
            client_secret=key[1],
            redirect_uri=key[2],
            scope=SPOTIFY_SCOPE,
            open_browser=True,
            cache_path=str(BASE_DIR / ".spotify_cache"),
        )
        _auth_key = key
        _sp = None
    return _auth


def spotify_connected():
    try:
        with _sp_lock:
            auth = _get_auth()
            return bool(auth.validate_token(auth.cache_handler.get_cached_token()))
    except Exception:
        return False


def connect_spotify_blocking():
    """Opens the browser login. Called from the window's Connect button."""
    with _sp_lock:
        auth = _get_auth()
    auth.get_access_token(as_dict=False)
    return True


def spotify_client():
    global _sp
    with _sp_lock:
        auth = _get_auth()
        if not auth.validate_token(auth.cache_handler.get_cached_token()):
            raise SpotifyNotReady("Click 'Connect Spotify' in the PulseDeck Server window")
        if _sp is None:
            _sp = spotipy.Spotify(auth_manager=auth, requests_timeout=6)
        return _sp


def spotify_snapshot():
    try:
        cur = spotify_client().current_playback()
    except SpotifyNotReady as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)[:100]}
    if not cur or not cur.get("item"):
        return {"active": False}
    item = cur["item"]
    return {
        "active": True,
        "playing": bool(cur.get("is_playing")),
        "track": item.get("name"),
        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
        "album": item.get("album", {}).get("name"),
        "progress_ms": cur.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "volume_percent": (cur.get("device") or {}).get("volume_percent"),
    }


def spotify_action(fn):
    try:
        fn(spotify_client())
        return jsonify({"ok": True})
    except SpotifyNotReady as e:
        return jsonify({"error": str(e)}), 400
    except spotipy.SpotifyException as e:
        if e.http_status == 404:
            msg = "No active Spotify device - open Spotify and play something"
        elif e.http_status == 403:
            msg = "Spotify Premium is needed to control playback"
        else:
            msg = (e.msg or str(e))[:100]
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": str(e)[:100]}), 500


@app.route("/spotify/now")
def spotify_now():
    return jsonify(spotify_snapshot())


@app.route("/spotify/play", methods=["POST"])
def spotify_play():
    return spotify_action(lambda sp: sp.start_playback())


@app.route("/spotify/pause", methods=["POST"])
def spotify_pause():
    return spotify_action(lambda sp: sp.pause_playback())


@app.route("/spotify/next", methods=["POST"])
def spotify_next():
    return spotify_action(lambda sp: sp.next_track())


@app.route("/spotify/previous", methods=["POST"])
def spotify_previous():
    return spotify_action(lambda sp: sp.previous_track())


@app.route("/spotify/volume", methods=["POST"])
def spotify_volume():
    pct = int((request.get_json(silent=True) or {}).get("percent", 50))
    return spotify_action(lambda sp: sp.volume(max(0, min(100, pct))))


# ---------------------------------------------------------------------------
# Steam
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
_steam_cache = (0.0, None)


def steam_snapshot():
    global _steam_cache
    if not CONFIG["steam_api_key"] or not CONFIG["steam_id"]:
        return {"error": "Steam not set up (optional)"}
    if requests is None:
        return {"error": "requests library missing"}
    ts, data = _steam_cache
    if data is not None and time.time() - ts < 15:
        return data
    try:
        r = requests.get(
            "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
            params={"key": CONFIG["steam_api_key"], "steamids": CONFIG["steam_id"]},
            timeout=5,
        )
        if r.status_code in (401, 403):
            data = {"error": "Steam API key rejected"}
        else:
            r.raise_for_status()
            players = r.json().get("response", {}).get("players", [])
            if not players:
                data = {"error": "No Steam profile found - check Steam ID"}
            else:
                p = players[0]
                data = {
                    "persona_name": p.get("personaname"),
                    "status": STEAM_STATE_MAP.get(p.get("personastate"), "Unknown"),
                    "in_game": p.get("gameextrainfo"),
                }
    except Exception as e:
        data = {"error": f"Steam unreachable: {str(e)[:60]}"}
    _steam_cache = (time.time(), data)
    return data


@app.route("/steam/status")
def steam_status():
    return jsonify(steam_snapshot())


# ---------------------------------------------------------------------------
# Live session timer (watches for game .exe files running on this PC)
# ---------------------------------------------------------------------------

_running_since = {}


def live_sessions():
    tracked = CONFIG.get("tracked_processes", {})
    try:
        running = {p.info["name"].lower() for p in psutil.process_iter(["name"]) if p.info.get("name")}
    except Exception:
        running = set()
    now = time.time()
    for game, exe in tracked.items():
        is_running = exe.lower() in running
        if is_running and game not in _running_since:
            _running_since[game] = now
        elif not is_running and game in _running_since:
            del _running_since[game]
    for game in list(_running_since):
        if game not in tracked:
            del _running_since[game]
    return [{"game": g, "seconds": int(now - t)} for g, t in _running_since.items()]


@app.route("/playtime/live")
def playtime_live():
    return jsonify({"sessions": live_sessions()})


# ---------------------------------------------------------------------------
# PC media keys - controls YouTube (any browser), Spotify app, VLC, etc.
# ---------------------------------------------------------------------------

MEDIA_KEYS = {
    "playpause": 0xB3,
    "next": 0xB0,
    "prev": 0xB1,
    "mute": 0xAD,
    "voldown": 0xAE,
    "volup": 0xAF,
}


def press_key(vk, times=1):
    import ctypes

    user32 = ctypes.windll.user32  # Windows only
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)  # key up


@app.route("/media/<action>", methods=["POST"])
def media(action):
    vk = MEDIA_KEYS.get(action)
    if vk is None:
        return jsonify({"error": "unknown action"}), 404
    times = int((request.get_json(silent=True) or {}).get("times", 1))
    try:
        press_key(vk, max(1, min(25, times)))
    except Exception:
        return jsonify({"error": "Media keys only work on Windows"}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# One call for the whole dashboard + misc
# ---------------------------------------------------------------------------


@app.route("/state")
def state():
    return jsonify(
        {
            "spotify": spotify_snapshot(),
            "steam": steam_snapshot(),
            "sessions": live_sessions(),
        }
    )


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


def running_process_names():
    names = set()
    for p in psutil.process_iter(["name"]):
        n = p.info.get("name")
        if n and n.lower().endswith(".exe"):
            names.add(n)
    return sorted(names, key=str.lower)


# ---------------------------------------------------------------------------
# Server thread (start / stop from the window)
# ---------------------------------------------------------------------------


class ServerThread:
    def __init__(self):
        self.srv = None
        self.thread = None

    @property
    def running(self):
        return self.srv is not None

    def start(self, port):
        from werkzeug.serving import make_server

        self.srv = make_server("0.0.0.0", port, app, threaded=True)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.srv:
            try:
                self.srv.shutdown()
                self.srv.server_close()
            finally:
                self.srv = None


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

BG = "#0F1117"
CARD = "#181B24"
CARD2 = "#252A3A"
FG = "#F2F3F7"
MUTED = "#8B90A0"
ACCENT = "#6C63FF"
GREEN = "#1DB954"
RED = "#FF5C5C"


def run_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("PulseDeck Server")
    root.geometry("580x780")
    root.minsize(540, 700)
    root.configure(bg=BG)

    server = ServerThread()
    ui_queue = queue.Queue()

    # ---- styling -----------------------------------------------------
    st = ttk.Style()
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=FG, fieldbackground=CARD2, font=("Segoe UI", 10))
    st.configure("Card.TFrame", background=CARD)
    st.configure("Card.TLabel", background=CARD, foreground=FG)
    st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
    st.configure("Head.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9, "bold"))
    st.configure("TButton", background=CARD2, foreground=FG, borderwidth=0, padding=(12, 7))
    st.map("TButton", background=[("active", "#323850")])
    st.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Segoe UI", 10, "bold"))
    st.map("Accent.TButton", background=[("active", "#5A52E0")])
    st.configure("TEntry", fieldbackground=CARD2, foreground=FG, insertcolor=FG, borderwidth=0, padding=7)
    st.configure("TCombobox", fieldbackground=CARD2, background=CARD2, foreground=FG, arrowcolor=FG, borderwidth=0, padding=6)
    st.configure("TNotebook", background=BG, borderwidth=0)
    st.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=(18, 9), borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", CARD2)], foreground=[("selected", FG)])

    # ---- header ------------------------------------------------------
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=18, pady=(16, 10))
    tk.Label(header, text="PulseDeck", bg=BG, fg=FG, font=("Segoe UI", 20, "bold")).pack(side="left")
    tk.Label(header, text="  Server", bg=BG, fg=MUTED, font=("Segoe UI", 12)).pack(side="left", pady=(7, 0))
    status_lbl = tk.Label(header, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10, "bold"))
    status_lbl.pack(side="right")

    # ---- connect card ------------------------------------------------
    card = ttk.Frame(root, style="Card.TFrame", padding=18)
    card.pack(fill="x", padx=16, pady=(0, 12))
    card.columnconfigure(1, weight=1)
    ttk.Label(card, text="CONNECT YOUR PHONE", style="Head.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")

    ip_var = tk.StringVar(value=local_ip())
    port_var = tk.StringVar(value=str(CONFIG["port"]))
    code_var = tk.StringVar(value=CONFIG["auth_token"])

    def copy(text):
        root.clipboard_clear()
        root.clipboard_append(text)
        msg_var.set("Copied")

    ttk.Label(card, text="PC address", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
    tk.Label(card, textvariable=ip_var, bg=CARD, fg=FG, font=("Consolas", 16, "bold")).grid(row=1, column=1, sticky="w", padx=14, pady=(12, 0))
    ttk.Button(card, text="Copy", command=lambda: copy(ip_var.get())).grid(row=1, column=2, pady=(12, 0))

    ttk.Label(card, text="Port", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))
    tk.Label(card, textvariable=port_var, bg=CARD, fg=FG, font=("Consolas", 16, "bold")).grid(row=2, column=1, sticky="w", padx=14, pady=(6, 0))
    ttk.Button(card, text="Copy", command=lambda: copy(port_var.get())).grid(row=2, column=2, pady=(6, 0))

    ttk.Label(card, text="Code", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 0))
    tk.Label(card, textvariable=code_var, bg=CARD, fg=ACCENT, font=("Consolas", 30, "bold")).grid(row=3, column=1, sticky="w", padx=14, pady=(6, 0))
    code_btns = ttk.Frame(card, style="Card.TFrame")
    code_btns.grid(row=3, column=2, pady=(10, 0))

    def regen_code():
        CONFIG["auth_token"] = new_code()
        save_config()
        code_var.set(CONFIG["auth_token"])
        msg_var.set("New code made - update it in the phone app")

    ttk.Button(code_btns, text="Copy", command=lambda: copy(code_var.get())).pack(side="left")
    ttk.Button(code_btns, text="New", command=regen_code).pack(side="left", padx=(6, 0))

    seen_lbl = ttk.Label(card, text="Phone: not connected yet", style="Muted.TLabel")
    seen_lbl.grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))
    toggle_btn = ttk.Button(card, text="Stop server")
    toggle_btn.grid(row=4, column=2, pady=(14, 0))

    # ---- settings tabs -----------------------------------------------
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=16, pady=(0, 8))

    def tab(title):
        f = ttk.Frame(nb, style="Card.TFrame", padding=18)
        f.columnconfigure(0, weight=1)
        nb.add(f, text=title)
        return f

    def field(parent, label, var, row, show=None):
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(8, 2))
        e = ttk.Entry(parent, textvariable=var, show=show)
        e.grid(row=row + 1, column=0, sticky="ew")
        return e

    # Spotify
    sp_tab = tab("Spotify")
    sp_id = tk.StringVar(value=CONFIG["spotify_client_id"])
    sp_secret = tk.StringVar(value=CONFIG["spotify_client_secret"])
    sp_status = tk.StringVar(value="")
    field(sp_tab, "Client ID", sp_id, 0)
    field(sp_tab, "Client secret", sp_secret, 2, show="•")
    ttk.Label(sp_tab, text="Redirect URI to add in your Spotify app:", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 2))
    tk.Label(sp_tab, text=CONFIG["spotify_redirect_uri"], bg=CARD, fg=FG, font=("Consolas", 10)).grid(row=5, column=0, sticky="w")
    sp_btns = ttk.Frame(sp_tab, style="Card.TFrame")
    sp_btns.grid(row=6, column=0, sticky="w", pady=(16, 0))
    ttk.Label(sp_tab, textvariable=sp_status, style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=(10, 0))

    # Steam
    st_tab = tab("Steam")
    steam_key = tk.StringVar(value=CONFIG["steam_api_key"])
    steam_id = tk.StringVar(value=CONFIG["steam_id"])
    field(st_tab, "API key (optional)", steam_key, 0, show="•")
    field(st_tab, "Steam ID (64-bit number)", steam_id, 2)
    ttk.Label(st_tab, text="Your Steam profile's game details must be Public.", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 0))
    ttk.Button(st_tab, text="Get a Steam API key", command=lambda: webbrowser.open("https://steamcommunity.com/dev/apikey")).grid(row=5, column=0, sticky="w", pady=(12, 0))

    # Games
    g_tab = tab("Games")
    ttk.Label(g_tab, text="Games to time (the phone shows how long each has been running)", style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
    listbox = tk.Listbox(g_tab, height=6, bg=CARD2, fg=FG, selectbackground=ACCENT, borderwidth=0, highlightthickness=0, font=("Segoe UI", 10))
    listbox.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 8))
    g_tab.rowconfigure(1, weight=1)
    g_name = tk.StringVar()
    g_exe = tk.StringVar()
    ttk.Label(g_tab, text="Game name", style="Muted.TLabel").grid(row=2, column=0, sticky="w")
    ttk.Entry(g_tab, textvariable=g_name).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 6))
    ttk.Label(g_tab, text="Program (.exe) - start the game, then press Refresh", style="Muted.TLabel").grid(row=4, column=0, sticky="w")
    combo = ttk.Combobox(g_tab, textvariable=g_exe, values=[])
    combo.grid(row=5, column=0, sticky="ew", pady=(2, 0))

    def refresh_procs():
        combo["values"] = running_process_names()

    ttk.Button(g_tab, text="Refresh", command=refresh_procs).grid(row=5, column=1, padx=(8, 0))

    def refresh_list():
        listbox.delete(0, "end")
        for n, e in CONFIG["tracked_processes"].items():
            listbox.insert("end", f"{n}   -   {e}")

    def add_game():
        n, e = g_name.get().strip(), g_exe.get().strip()
        if not n or not e:
            messagebox.showinfo("PulseDeck", "Enter a game name and pick its .exe")
            return
        CONFIG["tracked_processes"][n] = e
        save_config()
        g_name.set("")
        g_exe.set("")
        refresh_list()

    def remove_game():
        sel = listbox.curselection()
        if not sel:
            return
        name = listbox.get(sel[0]).split("   -   ")[0]
        CONFIG["tracked_processes"].pop(name, None)
        save_config()
        refresh_list()

    g_btns = ttk.Frame(g_tab, style="Card.TFrame")
    g_btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))
    ttk.Button(g_btns, text="Add game", style="Accent.TButton", command=add_game).pack(side="left")
    ttk.Button(g_btns, text="Remove selected", command=remove_game).pack(side="left", padx=(8, 0))
    refresh_list()
    refresh_procs()

    # General
    gen_tab = tab("General")
    port_edit = tk.StringVar(value=str(CONFIG["port"]))
    field(gen_tab, "Port", port_edit, 0)
    ttk.Label(gen_tab, text="If Windows asks about the firewall, allow it on private networks.", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(14, 0))

    # ---- footer ------------------------------------------------------
    msg_var = tk.StringVar(value="")
    footer = tk.Frame(root, bg=BG)
    footer.pack(fill="x", padx=16, pady=(0, 14))
    tk.Label(footer, textvariable=msg_var, bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")

    def start_server():
        try:
            server.start(CONFIG["port"])
        except OSError as e:
            messagebox.showerror("PulseDeck", f"Could not start on port {CONFIG['port']}.\nIs another copy already running?\n\n{e}")

    def toggle_server():
        if server.running:
            server.stop()
        else:
            start_server()

    toggle_btn.configure(command=toggle_server)

    def save_settings(quiet=False):
        CONFIG["spotify_client_id"] = sp_id.get().strip()
        CONFIG["spotify_client_secret"] = sp_secret.get().strip()
        CONFIG["steam_api_key"] = steam_key.get().strip()
        CONFIG["steam_id"] = steam_id.get().strip()
        try:
            new_port = int(port_edit.get().strip())
            if not 1024 <= new_port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showwarning("PulseDeck", "Port must be a number from 1024 to 65535")
            return False
        port_changed = new_port != CONFIG["port"]
        CONFIG["port"] = new_port
        save_config()
        reset_spotify()
        global _steam_cache
        _steam_cache = (0.0, None)
        port_var.set(str(new_port))
        if port_changed and server.running:
            server.stop()
            start_server()
        if not quiet:
            msg_var.set("Saved")
        return True

    ttk.Button(footer, text="Save settings", style="Accent.TButton", command=save_settings).pack(side="right")

    def connect_spotify():
        if not save_settings(quiet=True):
            return
        if not CONFIG["spotify_client_id"] or not CONFIG["spotify_client_secret"]:
            messagebox.showinfo("PulseDeck", "Enter your Spotify Client ID and secret first")
            return
        sp_status.set("Finish logging in in your browser...")

        def work():
            try:
                connect_spotify_blocking()
                result = "Connected"
            except Exception as e:
                result = f"Failed: {str(e)[:80]}"
            ui_queue.put(lambda: sp_status.set(result))

        threading.Thread(target=work, daemon=True).start()

    ttk.Button(sp_btns, text="Connect Spotify", style="Accent.TButton", command=connect_spotify).pack(side="left")
    ttk.Button(sp_btns, text="Spotify dashboard", command=lambda: webbrowser.open("https://developer.spotify.com/dashboard")).pack(side="left", padx=(8, 0))
    sp_status.set("Connected" if spotify_connected() else "Not connected yet")

    # ---- periodic update --------------------------------------------
    tick_n = [0]

    def tick():
        while True:
            try:
                ui_queue.get_nowait()()
            except queue.Empty:
                break
        if server.running:
            status_lbl.configure(text="●  Running", fg=GREEN)
            toggle_btn.configure(text="Stop server")
        else:
            status_lbl.configure(text="●  Stopped", fg=RED)
            toggle_btn.configure(text="Start server")
        if last_seen:
            ago = int(time.time() - last_seen)
            seen_lbl.configure(text="Phone: connected" if ago < 8 else f"Phone: last seen {ago}s ago")
        tick_n[0] += 1
        if tick_n[0] % 5 == 0:
            ip_var.set(local_ip())
        root.after(1000, tick)

    def on_close():
        server.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    start_server()
    tick()
    root.mainloop()


if __name__ == "__main__":
    run_gui()
