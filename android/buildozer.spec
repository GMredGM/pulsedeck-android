[app]
title = PulseDeck
package.name = pulsedeck
package.domain = org.yourname
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 0.1
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,requests,urllib3,idna,charset-normalizer,certifi
p4a.branch = v2024.01.21
orientation = all
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE
android.api = 33
android.minapi = 21
android.ndk = 25b
android.archs = armeabi-v7a, arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
