"""
PulseDeck - Android remote
--------------------------
Talks to the PulseDeck Server running on your PC (same Wi-Fi) and shows /
controls Spotify, PC media (YouTube etc.), your Steam status and live game
session time. Layout adapts: one column in portrait, two in landscape.
"""

import json
import threading
import time
from pathlib import Path

import requests
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, Line, Rectangle, RoundedRectangle, Triangle
from kivy.metrics import dp, sp
from kivy.properties import ListProperty, NumericProperty, StringProperty
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import escape_markup
from kivy.utils import get_color_from_hex as hexc

# ---- palette ------------------------------------------------------------
BG = hexc("#0F1117")
CARD = hexc("#181B24")
CARD2 = hexc("#252A3A")
TRACK = hexc("#2C3144")
TEXT = hexc("#F2F3F7")
MUTED = hexc("#8B90A0")
ACCENT = hexc("#6C63FF")
GREEN = hexc("#1DB954")
AMBER = hexc("#F5A524")
RED = hexc("#FF5C5C")
HEX = {"green": "1DB954", "red": "FF5C5C", "amber": "F5A524", "muted": "8B90A0", "accent": "8F88FF"}


# ---- settings (saved in the app's private folder) -----------------------
def settings_path():
    return Path(App.get_running_app().user_data_dir) / "settings.json"


def load_settings():
    try:
        return json.loads(settings_path().read_text())
    except Exception:
        return {"host": "", "port": "5000", "token": ""}


def save_settings(data):
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


def fmt_clock(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


def fmt_long(total):
    h, rem = divmod(int(total), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ---- small reusable widgets --------------------------------------------
class Txt(Label):
    """Left-aligned label that wraps and sizes its own height."""

    def __init__(self, **kw):
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "middle")
        kw.setdefault("color", TEXT)
        kw.setdefault("font_size", sp(15))
        super().__init__(size_hint_y=None, **kw)
        self.bind(width=self._on_width, texture_size=self._on_tex)

    def _on_width(self, *_):
        self.text_size = (self.width, None)

    def _on_tex(self, *_):
        self.height = self.texture_size[1] + dp(2)


class Card(BoxLayout):
    def __init__(self, title, accent=ACCENT, **kw):
        super().__init__(orientation="vertical", padding=dp(16), spacing=dp(10), size_hint_y=None, **kw)
        self.bind(minimum_height=self.setter("height"))
        with self.canvas.before:
            Color(*CARD)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(20)])
        self.bind(pos=self._sync, size=self._sync)
        self.add_widget(Txt(text=title.upper(), font_size=sp(12), bold=True, color=accent))

    def _sync(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size


class RoundButton(ButtonBehavior, Label):
    bg = ListProperty(CARD2)

    def __init__(self, **kw):
        kw.setdefault("color", TEXT)
        kw.setdefault("font_size", sp(14))
        kw.setdefault("bold", True)
        super().__init__(**kw)
        with self.canvas.before:
            self._c = Color(*self.bg)
            self._r = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(14)])
        self.bind(pos=self._u, size=self._u, state=self._u, bg=self._u)

    def _u(self, *_):
        self._r.pos = self.pos
        self._r.size = self.size
        base = self.bg
        if self.state == "down":
            self._c.rgba = [min(1, base[0] * 1.3), min(1, base[1] * 1.3), min(1, base[2] * 1.3), 1]
        else:
            self._c.rgba = base


class IconButton(ButtonBehavior, Widget):
    """Round button with a glyph drawn from shapes (no font needed)."""

    kind = StringProperty("play")
    bg = ListProperty(CARD2)
    fg = ListProperty(TEXT)

    def __init__(self, **kw):
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dp(56), dp(56)))
        kw.setdefault("pos_hint", {"center_y": 0.5})
        super().__init__(**kw)
        self.bind(pos=self.redraw, size=self.redraw, kind=self.redraw, state=self.redraw, bg=self.redraw, fg=self.redraw)
        self.redraw()

    def redraw(self, *_):
        self.canvas.clear()
        d = min(self.width, self.height)
        cx, cy = self.center
        u = d * 0.2
        base = self.bg
        if self.state == "down":
            base = [min(1, base[0] * 1.3), min(1, base[1] * 1.3), min(1, base[2] * 1.3), 1]
        k = self.kind
        with self.canvas:
            Color(*base)
            Ellipse(pos=(cx - d / 2, cy - d / 2), size=(d, d))
            Color(*self.fg)
            if k == "play":
                Triangle(points=[cx - u * 0.6, cy - u * 1.1, cx - u * 0.6, cy + u * 1.1, cx + u * 1.1, cy])
            elif k == "pause":
                Rectangle(pos=(cx - u * 0.9, cy - u), size=(u * 0.65, u * 2))
                Rectangle(pos=(cx + u * 0.25, cy - u), size=(u * 0.65, u * 2))
            elif k == "next":
                Triangle(points=[cx - u, cy - u * 0.9, cx - u, cy + u * 0.9, cx + u * 0.5, cy])
                Rectangle(pos=(cx + u * 0.6, cy - u * 0.9), size=(u * 0.35, u * 1.8))
            elif k == "prev":
                Triangle(points=[cx + u, cy - u * 0.9, cx + u, cy + u * 0.9, cx - u * 0.5, cy])
                Rectangle(pos=(cx - u * 0.95, cy - u * 0.9), size=(u * 0.35, u * 1.8))
            elif k in ("plus", "minus"):
                Rectangle(pos=(cx - u, cy - u * 0.2), size=(u * 2, u * 0.4))
                if k == "plus":
                    Rectangle(pos=(cx - u * 0.2, cy - u), size=(u * 0.4, u * 2))
            elif k == "mute":
                Rectangle(pos=(cx - u * 1.3, cy - u * 0.45), size=(u * 0.7, u * 0.9))
                Triangle(points=[cx - u * 0.6, cy + u * 0.45, cx - u * 0.6, cy - u * 0.45, cx + u * 0.4, cy - u])
                Triangle(points=[cx - u * 0.6, cy + u * 0.45, cx + u * 0.4, cy - u, cx + u * 0.4, cy + u])
                Line(points=[cx + u * 0.8, cy + u * 0.6, cx + u * 1.6, cy - u * 0.6], width=dp(1.5))
                Line(points=[cx + u * 0.8, cy - u * 0.6, cx + u * 1.6, cy + u * 0.6], width=dp(1.5))


class Bar(Widget):
    value = NumericProperty(0)
    bar_color = ListProperty(GREEN)

    def __init__(self, **kw):
        super().__init__(size_hint_y=None, height=dp(6), **kw)
        self.bind(pos=self.draw, size=self.draw, value=self.draw, bar_color=self.draw)

    def draw(self, *_):
        self.canvas.clear()
        r = self.height / 2
        with self.canvas:
            Color(*TRACK)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[r])
            if self.value > 0:
                Color(*self.bar_color)
                w = max(self.height, self.width * min(1, self.value))
                RoundedRectangle(pos=self.pos, size=(w, self.height), radius=[r])


class VolSlider(Slider):
    touching = False
    on_commit = None

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self.touching = True
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        was = self.touching
        self.touching = False
        result = super().on_touch_up(touch)
        if was and self.on_commit:
            self.on_commit(int(self.value))
        return result


def center_row(widgets, height=dp(68), spacing=dp(16)):
    row = BoxLayout(size_hint_y=None, height=height, spacing=spacing)
    row.add_widget(Widget())
    for w in widgets:
        row.add_widget(w)
    row.add_widget(Widget())
    return row


def styled_input(**kw):
    return TextInput(
        multiline=False,
        write_tab=False,
        size_hint_y=None,
        height=dp(46),
        font_size=sp(16),
        padding=[dp(12), dp(12), dp(12), dp(12)],
        background_normal="",
        background_active="",
        background_color=CARD2,
        foreground_color=TEXT,
        cursor_color=ACCENT,
        hint_text_color=MUTED,
        **kw,
    )


# ---- main screen -----------------------------------------------------------
class Dashboard(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", padding=[dp(12), dp(8), dp(12), 0], spacing=dp(8), **kw)
        self.settings = load_settings()
        self.inflight = False
        self.sp_playing = False
        self.sessions = []
        self.sessions_t0 = time.time()

        # top bar
        top = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(10))
        title = Label(text="PulseDeck", font_size=sp(22), bold=True, halign="left", valign="middle", size_hint_x=None, width=dp(130), color=TEXT)
        title.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.status = Label(text="", markup=True, font_size=sp(13), halign="right", valign="middle", color=TEXT)
        self.status.bind(size=lambda w, s: setattr(w, "text_size", s))
        settings_btn = RoundButton(text="Settings", size_hint=(None, None), size=(dp(92), dp(38)), pos_hint={"center_y": 0.5})
        settings_btn.bind(on_release=self.open_settings)
        top.add_widget(title)
        top.add_widget(self.status)
        top.add_widget(settings_btn)
        self.add_widget(top)

        # scrolling cards
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        self.grid = GridLayout(cols=1, spacing=dp(12), padding=[0, 0, 0, dp(16)], size_hint_y=None)
        self.grid.bind(minimum_height=self.grid.setter("height"))
        scroll.add_widget(self.grid)
        self.add_widget(scroll)

        self.build_spotify()
        self.build_media()
        self.build_steam()
        self.build_session()

        Window.bind(size=self.relayout)
        self.relayout()
        Clock.schedule_interval(self.poll, 2)
        Clock.schedule_interval(self.tick, 1)
        Clock.schedule_once(self.first_run, 0.4)

    # -- layout -------------------------------------------------------------
    def relayout(self, *_):
        wide = Window.width > Window.height and Window.width > dp(560)
        self.grid.cols = 2 if wide else 1

    def build_spotify(self):
        c = Card("Spotify", GREEN)
        self.sp_track = Txt(text="Not connected", font_size=sp(19), bold=True)
        self.sp_artist = Txt(text="", color=MUTED, font_size=sp(14))
        self.sp_bar = Bar()
        self.sp_time = Txt(text="0:00 / 0:00", color=MUTED, font_size=sp(12))
        c.add_widget(self.sp_track)
        c.add_widget(self.sp_artist)
        c.add_widget(self.sp_bar)
        c.add_widget(self.sp_time)

        prev = IconButton(kind="prev")
        self.sp_play = IconButton(kind="play", bg=GREEN, fg=BG, size=(dp(68), dp(68)))
        nxt = IconButton(kind="next")
        prev.bind(on_release=lambda *_: self.spotify_cmd("previous"))
        self.sp_play.bind(on_release=self.spotify_toggle)
        nxt.bind(on_release=lambda *_: self.spotify_cmd("next"))
        c.add_widget(center_row([prev, self.sp_play, nxt], height=dp(74)))

        self.vol_label = Txt(text="Volume", color=MUTED, font_size=sp(12))
        self.vol = VolSlider(min=0, max=100, value=50, step=1, size_hint_y=None, height=dp(36),
                             value_track=True, value_track_color=GREEN, cursor_size=(dp(24), dp(24)))
        self.vol.bind(value=lambda w, v: setattr(self.vol_label, "text", f"Volume  {int(v)}%"))
        self.vol.on_commit = lambda v: self.api("POST", "/spotify/volume", {"percent": v}, self.action_done)
        c.add_widget(self.vol_label)
        c.add_widget(self.vol)
        self.grid.add_widget(c)

    def build_media(self):
        c = Card("PC media  -  YouTube & more", ACCENT)
        c.add_widget(Txt(text="Controls whatever is playing on your PC: YouTube in any browser, a video player, music apps.", color=MUTED, font_size=sp(13)))
        prev = IconButton(kind="prev")
        play = IconButton(kind="play", bg=ACCENT, size=(dp(68), dp(68)))
        nxt = IconButton(kind="next")
        prev.bind(on_release=lambda *_: self.media("prev"))
        play.bind(on_release=lambda *_: self.media("playpause"))
        nxt.bind(on_release=lambda *_: self.media("next"))
        c.add_widget(center_row([prev, play, nxt], height=dp(74)))
        vdown = IconButton(kind="minus")
        mute = IconButton(kind="mute")
        vup = IconButton(kind="plus")
        vdown.bind(on_release=lambda *_: self.media("voldown", 3))
        mute.bind(on_release=lambda *_: self.media("mute"))
        vup.bind(on_release=lambda *_: self.media("volup", 3))
        c.add_widget(center_row([vdown, mute, vup], height=dp(62)))
        self.grid.add_widget(c)

    def build_steam(self):
        c = Card("Steam", hexc("#66C0F4"))
        self.steam_name = Txt(text="-", font_size=sp(19), bold=True)
        self.steam_status = Txt(text="", markup=True, color=MUTED, font_size=sp(14))
        c.add_widget(self.steam_name)
        c.add_widget(self.steam_status)
        self.grid.add_widget(c)

    def build_session(self):
        c = Card("Game session", AMBER)
        self.sess_time = Txt(text="--", font_size=sp(34), bold=True)
        self.sess_game = Txt(text="No tracked game running", color=MUTED, font_size=sp(14))
        c.add_widget(self.sess_time)
        c.add_widget(self.sess_game)
        self.grid.add_widget(c)

    # -- networking ----------------------------------------------------------
    def api(self, method, path, body=None, cb=None):
        host = self.settings.get("host", "").strip()
        url = f"http://{host}:{self.settings.get('port') or '5000'}{path}"
        headers = {"X-Auth-Token": self.settings.get("token", "")}

        def run():
            data, err = None, None
            try:
                r = requests.request(method, url, headers=headers, json=body if method != "GET" else None, timeout=4)
                try:
                    data = r.json()
                except ValueError:
                    data = None
                if r.status_code >= 400:
                    err = data.get("error") if isinstance(data, dict) else None
                    err = err or f"Error {r.status_code}"
            except requests.exceptions.Timeout:
                err = "PC not responding"
            except requests.exceptions.ConnectionError:
                err = "Can't reach PC - same Wi-Fi?"
            except Exception as e:
                err = str(e)[:60]
            if cb:
                Clock.schedule_once(lambda dt: cb(data, err), 0)

        threading.Thread(target=run, daemon=True).start()

    def set_status(self, text, ok):
        color = {True: "green", False: "red", None: "amber"}[ok]
        self.status.text = f"[color={HEX[color]}]●[/color]  {escape_markup(text)}"

    def poll(self, *_):
        if not self.settings.get("host"):
            self.set_status("Tap Settings to add your PC", None)
            return
        if self.inflight:
            return
        self.inflight = True
        self.api("GET", "/state", None, self.on_state)

    def on_state(self, data, err):
        self.inflight = False
        if err or not isinstance(data, dict):
            self.set_status(err or "No data", False)
            return
        self.set_status("Connected", True)
        self.apply_spotify(data.get("spotify") or {})
        self.apply_steam(data.get("steam") or {})
        self.sessions = data.get("sessions") or []
        self.sessions_t0 = time.time()
        self.render_sessions()

    # -- apply data ----------------------------------------------------------
    def apply_spotify(self, s):
        if "error" in s:
            self.sp_track.text = "Spotify not ready"
            self.sp_artist.text = s["error"]
            self.sp_bar.value = 0
            self.sp_time.text = ""
            self.sp_playing = False
            self.sp_play.kind = "play"
            return
        if not s.get("active"):
            self.sp_track.text = "Nothing playing"
            self.sp_artist.text = "Press play on a Spotify device"
            self.sp_bar.value = 0
            self.sp_time.text = ""
            self.sp_playing = False
            self.sp_play.kind = "play"
            return
        self.sp_playing = bool(s.get("playing"))
        self.sp_track.text = s.get("track") or ""
        self.sp_artist.text = s.get("artist") or ""
        dur = s.get("duration_ms") or 0
        prog = s.get("progress_ms") or 0
        self.sp_bar.value = (prog / dur) if dur else 0
        self.sp_time.text = f"{fmt_clock(prog)} / {fmt_clock(dur)}"
        self.sp_play.kind = "pause" if self.sp_playing else "play"
        vol = s.get("volume_percent")
        if vol is not None and not self.vol.touching:
            self.vol.value = vol

    def apply_steam(self, s):
        if "error" in s:
            self.steam_name.text = "Steam"
            self.steam_status.text = escape_markup(s["error"])
            return
        self.steam_name.text = s.get("persona_name") or "Steam"
        game = s.get("in_game")
        status = s.get("status") or "Unknown"
        if game:
            line = f"[color={HEX['accent']}]●[/color]  Playing {escape_markup(game)}"
        else:
            col = {"Online": "green", "Away": "amber", "Busy": "red", "Snooze": "amber"}.get(status, "muted")
            line = f"[color={HEX[col]}]●[/color]  {escape_markup(status)}"
        self.steam_status.text = line

    def render_sessions(self):
        if not self.sessions:
            self.sess_time.text = "--"
            self.sess_game.text = "No tracked game running"
            return
        el = time.time() - self.sessions_t0
        first = self.sessions[0]
        self.sess_time.text = fmt_long(first["seconds"] + el)
        lines = [first["game"]]
        for extra in self.sessions[1:]:
            lines.append(f"{extra['game']}  -  {fmt_long(extra['seconds'] + el)}")
        self.sess_game.text = "\n".join(lines)

    def tick(self, *_):
        if self.sessions:
            self.render_sessions()

    # -- actions ----------------------------------------------------------------
    def action_done(self, data, err):
        if err:
            self.set_status(err, False)
        Clock.schedule_once(self.poll, 0.7)

    def spotify_cmd(self, name):
        self.api("POST", f"/spotify/{name}", {}, self.action_done)

    def spotify_toggle(self, *_):
        if self.sp_playing:
            self.sp_playing = False
            self.sp_play.kind = "play"
            self.spotify_cmd("pause")
        else:
            self.sp_playing = True
            self.sp_play.kind = "pause"
            self.spotify_cmd("play")

    def media(self, action, times=1):
        self.api("POST", f"/media/{action}", {"times": times}, self.action_done)

    # -- settings ---------------------------------------------------------------
    def first_run(self, *_):
        if not self.settings.get("host"):
            self.open_settings()

    def open_settings(self, *_):
        box = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(6))
        box.add_widget(Txt(text="On your PC, open PulseDeck Server and copy the address, port and code shown there.", color=MUTED, font_size=sp(13)))
        host = styled_input(text=self.settings.get("host", ""), hint_text="e.g. 192.168.1.42")
        port = styled_input(text=self.settings.get("port", "5000"), hint_text="5000", input_filter="int")
        code = styled_input(text=self.settings.get("token", ""), hint_text="6-character code")
        for label, w in (("PC address", host), ("Port", port), ("Code", code)):
            box.add_widget(Txt(text=label, color=MUTED, font_size=sp(12)))
            box.add_widget(w)
        buttons = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        cancel = RoundButton(text="Cancel")
        save = RoundButton(text="Save", bg=ACCENT)
        buttons.add_widget(cancel)
        buttons.add_widget(save)
        box.add_widget(Widget(size_hint_y=None, height=dp(4)))
        box.add_widget(buttons)

        popup = Popup(
            title="Connect to your PC",
            content=box,
            size_hint=(0.92, None),
            height=dp(470),
            auto_dismiss=False,
            background="",
            background_color=CARD,
            separator_color=ACCENT,
            title_size=sp(17),
        )

        def do_save(*_):
            self.settings = {
                "host": host.text.strip(),
                "port": port.text.strip() or "5000",
                "token": code.text.strip().upper(),
            }
            save_settings(self.settings)
            popup.dismiss()
            self.poll()

        save.bind(on_release=do_save)
        cancel.bind(on_release=lambda *_: popup.dismiss())
        popup.open()


class PulseDeckApp(App):
    title = "PulseDeck"

    def build(self):
        Window.clearcolor = BG
        Window.softinput_mode = "below_target"
        return Dashboard()


if __name__ == "__main__":
    PulseDeckApp().run()
