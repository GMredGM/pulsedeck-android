"""
PulseDeck - Android App
-------------------------------
A Kivy app that polls your PC's helper server over your home Wi-Fi and
shows/controls Spotify, your Steam status, and live game session time.

Build into an APK with buildozer (see README.md for the exact steps -
this file alone does not produce an .apk; it needs to be compiled).
"""

import json
from pathlib import Path

import requests
from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput

SETTINGS_PATH = Path(App.get_running_app().user_data_dir if App.get_running_app() else ".") / "settings.json"


def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {"host": "", "port": "5000", "token": ""}


def save_settings(data):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data))


def fmt_seconds(total_seconds):
    h, rem = divmod(int(total_seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class RemoteUI(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", padding=16, spacing=10, **kwargs)
        self.settings = load_settings()

        self.status_label = Label(text="Not connected", size_hint_y=None, height=30)
        self.add_widget(self.status_label)

        self.add_widget(Label(text="[b]Spotify[/b]", markup=True, size_hint_y=None, height=24))
        self.spotify_label = Label(text="-", size_hint_y=None, height=50)
        self.add_widget(self.spotify_label)

        controls = BoxLayout(size_hint_y=None, height=50, spacing=8)
        for text, cb in [
            ("⏮", self.spotify_previous),
            ("⏯", self.spotify_toggle),
            ("⏭", self.spotify_next),
        ]:
            b = Button(text=text)
            b.bind(on_release=cb)
            controls.add_widget(b)
        self.add_widget(controls)

        self.add_widget(Label(text="[b]Steam[/b]", markup=True, size_hint_y=None, height=24))
        self.steam_label = Label(text="-", size_hint_y=None, height=40)
        self.add_widget(self.steam_label)

        self.add_widget(Label(text="[b]Live session time[/b]", markup=True, size_hint_y=None, height=24))
        self.playtime_label = Label(text="-", size_hint_y=None, height=60)
        self.add_widget(self.playtime_label)

        self.add_widget(Label(text="[b]YouTube[/b]", markup=True, size_hint_y=None, height=24))
        self.add_widget(
            Label(
                text="No remote-control API exists for this - see README",
                size_hint_y=None,
                height=40,
            )
        )

        settings_btn = Button(text="Settings (PC address / token)", size_hint_y=None, height=50)
        settings_btn.bind(on_release=self.open_settings)
        self.add_widget(settings_btn)

        Clock.schedule_interval(self.refresh, 3)

    # -- networking ---------------------------------------------------

    def base_url(self):
        host = self.settings.get("host", "")
        port = self.settings.get("port", "5000")
        return f"http://{host}:{port}"

    def headers(self):
        return {"X-Auth-Token": self.settings.get("token", "")}

    def get(self, path):
        try:
            r = requests.get(self.base_url() + path, headers=self.headers(), timeout=4)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            self.status_label.text = f"Connection error: {e}"
            return None

    def post(self, path, json_body=None):
        try:
            requests.post(self.base_url() + path, headers=self.headers(), json=json_body or {}, timeout=4)
        except Exception as e:
            self.status_label.text = f"Connection error: {e}"

    # -- refresh loop ---------------------------------------------------

    def refresh(self, *_):
        if not self.settings.get("host"):
            self.status_label.text = "Set your PC's address in Settings"
            return

        now = self.get("/spotify/now")
        if now:
            self.status_label.text = "Connected"
            if now.get("playing"):
                self.spotify_label.text = f"{now.get('track')} — {now.get('artist')}"
            else:
                self.spotify_label.text = "Paused / nothing playing"

        steam = self.get("/steam/status")
        if steam and "status" in steam:
            in_game = steam.get("in_game")
            self.steam_label.text = (
                f"{steam.get('persona_name')}: {steam.get('status')}"
                + (f" (playing {in_game})" if in_game else "")
            )

        live = self.get("/playtime/live")
        if live and "sessions" in live:
            sessions = live["sessions"]
            if sessions:
                self.playtime_label.text = "\n".join(
                    f"{s['game']}: {fmt_seconds(s['seconds'])}" for s in sessions
                )
            else:
                self.playtime_label.text = "No tracked game currently running"

    # -- controls ---------------------------------------------------

    def spotify_toggle(self, *_):
        now = self.get("/spotify/now")
        if now and now.get("playing"):
            self.post("/spotify/pause")
        else:
            self.post("/spotify/play")

    def spotify_next(self, *_):
        self.post("/spotify/next")

    def spotify_previous(self, *_):
        self.post("/spotify/previous")

    # -- settings popup ---------------------------------------------------

    def open_settings(self, *_):
        box = BoxLayout(orientation="vertical", padding=12, spacing=8)
        host_input = TextInput(text=self.settings.get("host", ""), hint_text="PC IP, e.g. 192.168.1.42", multiline=False)
        port_input = TextInput(text=self.settings.get("port", "5000"), hint_text="Port", multiline=False)
        token_input = TextInput(text=self.settings.get("token", ""), hint_text="Auth token from server console", multiline=False)
        save_btn = Button(text="Save", size_hint_y=None, height=50)

        box.add_widget(Label(text="PC IP address"))
        box.add_widget(host_input)
        box.add_widget(Label(text="Port"))
        box.add_widget(port_input)
        box.add_widget(Label(text="Auth token"))
        box.add_widget(token_input)
        box.add_widget(save_btn)

        popup = Popup(title="Settings", content=box, size_hint=(0.9, 0.7))

        def do_save(*_):
            self.settings = {
                "host": host_input.text.strip(),
                "port": port_input.text.strip() or "5000",
                "token": token_input.text.strip(),
            }
            save_settings(self.settings)
            popup.dismiss()

        save_btn.bind(on_release=do_save)
        popup.open()


class PulseDeckApp(App):
    def build(self):
        return RemoteUI()


if __name__ == "__main__":
    PulseDeckApp().run()
