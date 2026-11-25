# PulseDeck

A two-part project:

1. **`server/`** — a small Python server you run on your PC. It talks to
   Spotify, Steam, and watches which games are currently running.
2. **`android/`** — a Kivy app that becomes your Android app. It polls the
   PC server over Wi-Fi and shows Now Playing / Steam status / live session
   timer, with Spotify playback buttons.

I can't compile a `.apk` for you directly (that needs the Android SDK/NDK
and internet access, which this chat environment doesn't have), but the
build is a single command once you have the tools installed — see below.

## 1. Set up the PC server

```
cd server
pip install -r requirements.txt
python app.py
```

First run creates `server/config.json` and exits — fill in:

- `spotify_client_id` / `spotify_client_secret` — from a free app you
  register at https://developer.spotify.com/dashboard (add redirect URI
  `http://127.0.0.1:8888/callback`)
- `steam_api_key` — from https://steamcommunity.com/dev/apikey
- `steam_id` — your 64-bit SteamID (look it up at https://steamid.io)
- `tracked_processes` — map of game name → its `.exe` name, e.g.:
  ```json
  "tracked_processes": {
      "Hollow Knight": "hollow_knight.exe"
  }
  ```
  (Find the exe name in Task Manager while the game is running.)

Run `python app.py` again. It prints your PC's local IP, the port, and an
auth token — you'll enter all three into the Android app's Settings screen.

Leave this running in the background whenever you want the phone app to
work (it only works on your home Wi-Fi, by design — there's no internet
exposure or cloud relay here).

## 2. Build the Android app into an APK

You need a Linux environment (native Linux, or WSL on Windows, or a VM) with
`buildozer` installed, since it downloads and drives the Android SDK/NDK:

```
pip install buildozer cython
cd android
buildozer -v android debug
```

First build takes a while (downloading the Android SDK/NDK). The finished
file lands at `android/bin/pulsedeck-0.1-debug.apk` — copy it to your
phone and install it (you'll need to allow "install from unknown sources").

On first launch, open Settings in the app and enter the PC's IP, port, and
auth token from step 1.

## About YouTube

There's no public API that lets one device remote-control YouTube playback
on another device the way Spotify allows. Realistic options if you want
something here later:

- **Chromecast**: if you cast YouTube to a Chromecast-capable device, the
  Google Cast SDK *does* expose play/pause/skip — a very different, bigger
  project than this one.
- **Browser extension**: a Chrome extension on the PC could report
  "currently playing video title" to the helper server (so at least you'd
  *see* it in the app), but still couldn't reliably control playback.
- **Smart TV / YouTube app pairing**: similar story to Chromecast, API
  support varies a lot by device.

I left a placeholder spot for this in the Android UI so it's easy to wire
in later if you want to go one of those routes.

## Notes on the live session timer

It works by having the PC server check, every time the app asks, whether a
tracked `.exe` is in the running process list, and tracking when it first
noticed that. That means:

- It resets to 0 if you restart the game (that's a new "session").
- It only times games you've listed in `tracked_processes` — Steam itself
  doesn't expose a live "seconds played this session" value, so this is the
  practical workaround.
