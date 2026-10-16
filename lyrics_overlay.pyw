# -*- coding: utf-8 -*-
"""
Lyrics Overlay - Displays synced lyrics of the currently playing song
(YouTube, Spotify, etc.) in a small always-on-top window on Windows.

Setup (once): run  pip install winsdk musicxmatch_api pystray Pillow  in a terminal.
Run: double-click the file.
"""
import asyncio
import json
import os
import re
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import datetime, timezone
from tkinter import messagebox

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PStatus,
    )
except ImportError:
    r = tk.Tk()
    r.withdraw()
    messagebox.showerror(
        "Missing package",
        "The 'winsdk' package is not installed.\n\nOpen a terminal and run:\n\npip install winsdk",
    )
    raise SystemExit

# MusicXMatch import
try:
    from musicxmatch_api import MusixMatchAPI
    MUSICXMATCH_AVAILABLE = True
except ImportError:
    MUSICXMATCH_AVAILABLE = False

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".lyrics_overlay_settings.json")

THEMES = {
    "dark":  {"bg": "#14141c", "top": "#1d1d29", "text": "#cfcfe0", "active": "#ffd54a", "passive": "#7a7a90", "info": "#9a9ab5", "entry": "#25253a", "button": "#25253a", "button_active": "#3a3a55"},
    "light": {"bg": "#f0f0f0", "top": "#e0e0e0", "text": "#202020", "active": "#c0392b", "passive": "#808080", "info": "#606060", "entry": "#ffffff", "button": "#d0d0d0", "button_active": "#b0b0b0"},
    "blue":  {"bg": "#0a1929", "top": "#102a43", "text": "#b0c4de", "active": "#4fc3f7", "passive": "#607d8b", "info": "#78909c", "entry": "#1a3a5c", "button": "#1a3a5c", "button_active": "#2a5a8c"},
}

# Active line color options
COLORS = {
    "yellow": {"name": "🟡 Yellow", "code": "#ffd54a"},
    "orange": {"name": "🟠 Orange", "code": "#ff9f43"},
    "green":  {"name": "🟢 Green",  "code": "#4cd137"},
    "blue":   {"name": "🔵 Blue",   "code": "#4fc3f7"},
    "pink":   {"name": "🩷 Pink",   "code": "#ff6b9d"},
    "white":  {"name": "⚪ White",  "code": "#ffffff"},
    "red":    {"name": "🔴 Red",    "code": "#e74c3c"},
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


settings = load_settings()
theme_name = settings.get("theme", "dark")
THEME = THEMES.get(theme_name, THEMES["dark"])

# Load active color from settings
active_color_name = settings.get("active_color", "yellow")
ACTIVE_COLOR = COLORS.get(active_color_name, COLORS["yellow"])["code"]

MAX_DURATION = 600  # seconds: longer videos are not treated as songs

lock = threading.Lock()
state = {
    "title": "", "artist": "", "duration": 0.0, "position": 0.0,
    "pos_time": time.monotonic(), "playing": False, "has_media": False,
    "lyrics_times": [], "lyrics_lines": [], "message": "Waiting for music...",
    "manual": None, "token": 0, "key": None, "offset": 0.0,
    "rec_duration": 0, "scroll_offset": 0,
}


def clean_title(title, artist):
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)
    t = t.split("|")[0]
    t = re.sub(
        r"\b(official|video|music|lyrics?|lyric|audio|hd|4k|clip|remix|live)\b",
        " ", t, flags=re.I,
    )
    t = re.sub(r"\s+", " ", t).strip(" -–")
    a = re.sub(r"\s*-\s*Topic$|VEVO$", "", artist or "", flags=re.I).strip()
    if " - " in t:
        a2, tr = t.split(" - ", 1)
        return [f"{a2} {tr}".strip(), tr.strip()]
    return [f"{a} {t}".strip(), t]


def parse_lrc(text):
    times, lines = [], []
    for line in text.splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        content = re.sub(r"\[[^\]]*\]", "", line).strip()
        for m, s in tags:
            times.append(int(m) * 60 + float(s))
            lines.append(content)
    sorted_pairs = sorted(zip(times, lines))
    return [x[0] for x in sorted_pairs], [x[1] for x in sorted_pairs]


DURATION_TOLERANCE = 25


def search_musicxmatch(query):
    """Search MusicXMatch; return synced lyrics in LRC format if available."""
    if not MUSICXMATCH_AVAILABLE:
        return None
    try:
        api = MusixMatchAPI()
        results = api.search_tracks(query)
        if not results:
            return None
        track = results[0]
        track_id = track.get("track_id")
        if not track_id:
            return None
        subtitle = api.get_subtitle(track_id)
        if subtitle and subtitle.get("subtitle_body"):
            return subtitle["subtitle_body"]
    except Exception:
        pass
    return None


def search_lyrics(queries, duration):
    total = 0
    best_match = None

    # 1. Try lrclib.net first
    for q in queries:
        if not q:
            continue
        try:
            url = "https://lrclib.net/api/search?q=" + urllib.parse.quote(q)
            req = urllib.request.Request(url, headers={"User-Agent": "LyricsOverlay/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                results = json.loads(r.read().decode("utf-8"))
            total += len(results)
            for x in results:
                if not x.get("syncedLyrics"):
                    continue
                diff = abs((x.get("duration") or 0) - duration) if duration > 0 else 0
                if best_match is None or diff < best_match[0]:
                    best_match = (diff, x)
            if best_match and best_match[0] <= DURATION_TOLERANCE:
                break
        except Exception:
            pass

    if best_match and best_match[0] <= DURATION_TOLERANCE:
        rec_duration = int(best_match[1].get("duration") or 0)
        return best_match[1]["syncedLyrics"], "", rec_duration

    # 2. If not found in lrclib, try MusicXMatch
    for q in queries:
        if not q:
            continue
        lrc = search_musicxmatch(q)
        if lrc:
            return lrc, "", 0

    q0 = queries[0] if queries else ""
    if total == 0:
        return None, "No record found: «" + q0 + "»", 0
    return None, "Lyrics exist but are not synced: «" + q0 + "»", 0


def fetch_lyrics(token, queries, duration):
    rec_duration = 0
    try:
        lrc, info, rec_duration = search_lyrics(queries, duration)
        if lrc:
            times, lines = parse_lrc(lrc)
            msg = ""
        else:
            times, lines, msg = [], [], info
    except Exception:
        times, lines, msg = [], [], "Could not reach lyrics service"
    with lock:
        if state["token"] == token:
            state["lyrics_times"] = times
            state["lyrics_lines"] = lines
            state["message"] = msg
            state["rec_duration"] = rec_duration


def new_song(title, artist, duration, manual=None):
    with lock:
        state["token"] += 1
        token = state["token"]
        state["lyrics_times"], state["lyrics_lines"] = [], []
        state["offset"] = 0.0
        state["rec_duration"] = 0
        state["scroll_offset"] = 0
        if manual is None and duration > MAX_DURATION:
            state["message"] = "Doesn't look like a song (video too long)"
            return
        state["message"] = "Searching lyrics..."
    if manual:
        words = manual.split()
        queries = [manual] + [" ".join(words[k:]) for k in range(1, min(4, len(words) - 1) + 1)]
    else:
        queries = clean_title(title, artist)
    threading.Thread(target=fetch_lyrics, args=(token, queries, 0 if manual else duration), daemon=True).start()


async def poll_loop():
    mgr = await MediaManager.request_async()
    while True:
        try:
            sess = mgr.get_current_session()
            if sess is None:
                with lock:
                    state["has_media"] = False
                    state["playing"] = False
                    if state["key"] is not None:
                        state["key"] = None
                        state["message"] = "Waiting for music..."
                        state["lyrics_times"], state["lyrics_lines"] = [], []
            else:
                props = await sess.try_get_media_properties_async()
                title, artist = props.title or "", props.artist or ""
                tl = sess.get_timeline_properties()
                pos = tl.position.total_seconds()
                dur = (tl.end_time - tl.start_time).total_seconds()
                playing = sess.get_playback_info().playback_status == PStatus.PLAYING
                if playing:
                    try:
                        pos += max(0.0, (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds())
                    except Exception:
                        pass
                key = (title, artist)
                changed = False
                with lock:
                    state.update(title=title, artist=artist, duration=dur, position=pos,
                                 pos_time=time.monotonic(), playing=playing, has_media=True)
                    if key != state["key"]:
                        state["key"] = key
                        changed = state["manual"] is None
                        state["manual"] = None
                if changed and title:
                    new_song(title, artist, dur)
        except Exception:
            pass
        await asyncio.sleep(0.5)


threading.Thread(target=lambda: asyncio.run(poll_loop()), daemon=True).start()

# ---------- Window ----------
root = tk.Tk()
root.title("Lyrics Overlay")
root.overrideredirect(True)
root.attributes("-topmost", True)
root.attributes("-alpha", settings.get("opacity", 93) / 100)
root.configure(bg=THEME["bg"])

sw = root.winfo_screenwidth()
default_w, default_h = 480, 415
window_w = settings.get("width", default_w)
window_h = settings.get("height", default_h)
window_x = settings.get("x", sw - 500)
window_y = settings.get("y", 60)
root.geometry(f"{window_w}x{window_h}+{window_x}+{window_y}")

_drag = {"x": 0, "y": 0, "px": 0, "py": 0}


def drag_start(e):
    _drag["x"] = e.x_root - root.winfo_x()
    _drag["y"] = e.y_root - root.winfo_y()
    _drag["px"] = e.x_root
    _drag["py"] = e.y_root


def drag_move(e):
    new_x = e.x_root - _drag["x"]
    new_y = e.y_root - _drag["y"]
    root.geometry(f"+{new_x}+{new_y}")


def close_app():
    try:
        settings["x"] = root.winfo_x()
        settings["y"] = root.winfo_y()
        settings["width"] = root.winfo_width()
        settings["height"] = root.winfo_height()
        settings["opacity"] = opacity_scale.get()
        settings["theme"] = theme_name
        settings["active_color"] = active_color_name
        save_settings(settings)
    except Exception:
        pass
    if tray_icon:
        try:
            tray_icon.stop()
        except Exception:
            pass
    root.destroy()


# ---------- Widgets ----------
top_bar = tk.Frame(root, bg=THEME["top"])
top_bar.pack(fill="x")

close_btn = tk.Button(top_bar, text="✕", command=close_app, bg=THEME["top"], fg=THEME["text"],
          bd=0, activebackground="#c0392b", activeforeground="white",
          font=("Segoe UI", 10), padx=6)
close_btn.pack(side="right", padx=(0, 4))


def hide_window():
    root.withdraw()


hide_btn = tk.Button(top_bar, text="−", command=hide_window, bg=THEME["top"], fg=THEME["text"],
          bd=0, activebackground=THEME["button_active"], activeforeground=THEME["text"],
          font=("Segoe UI", 11, "bold"), padx=6)
hide_btn.pack(side="right", padx=(0, 2))

settings_btn = tk.Button(top_bar, text="⚙", bg=THEME["top"], fg=THEME["text"],
          bd=0, activebackground=THEME["button_active"], activeforeground=THEME["text"],
          font=("Segoe UI", 10), padx=6)
settings_btn.pack(side="right", padx=(0, 2))

title_label = tk.Label(top_bar, text="♪ Lyrics Overlay", bg=THEME["top"], fg=THEME["text"],
                  font=("Segoe UI", 10, "bold"), anchor="w")
title_label.pack(side="left", fill="x", expand=True, padx=8, pady=5)

body = tk.Frame(root, bg=THEME["bg"])
body.pack(fill="both", expand=True, padx=14, pady=8)

lyric_labels = []
for i in range(5):
    is_center = i == 2
    lb = tk.Label(
        body, text="", bg=THEME["bg"],
        fg=ACTIVE_COLOR if is_center else THEME["passive"],
        font=("Segoe UI", 17 if is_center else 11, "bold" if is_center else "normal"),
        wraplength=440, justify="center",
    )
    lb.pack(expand=True, fill="x", pady=2)
    lyric_labels.append(lb)

search_bar = tk.Frame(root, bg=THEME["bg"])
search_bar.pack(fill="x", padx=10, pady=(0, 8))
search_entry = tk.Entry(search_bar, bg=THEME["entry"], fg=THEME["text"], insertbackground=THEME["text"],
                 bd=0, font=("Segoe UI", 9))
search_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 4))


def manual_search():
    q = search_entry.get().strip()
    if not q:
        return
    with lock:
        state["manual"] = q
        duration = state["duration"]
    new_song("", "", duration, manual=q)


def auto_search():
    search_entry.delete(0, "end")
    with lock:
        t, a, d = state["title"], state["artist"], state["duration"]
        state["manual"] = None
        state["scroll_offset"] = 0
    if t:
        new_song(t, a, d)


def shift_time(d):
    with lock:
        state["offset"] += d


def make_button(txt, cmd, parent, **kw):
    return tk.Button(parent, text=txt, command=cmd, bg=THEME["button"], fg=THEME["text"],
                     bd=0, activebackground=THEME["button_active"], activeforeground=THEME["text"],
                     font=("Segoe UI", 9), padx=6, **kw)


make_button("Search", manual_search, search_bar).pack(side="left", padx=1)
make_button("Auto", auto_search, search_bar).pack(side="left", padx=1)
search_entry.bind("<Return>", lambda e: manual_search())


def reset_offset():
    with lock:
        state["offset"] = 0.0


def fmt_time(x):
    x = max(0, int(x))
    return str(x // 60) + ":" + str(x % 60).zfill(2)


control_bar = tk.Frame(root, bg=THEME["bg"])
control_bar.pack(fill="x", padx=10, pady=(0, 8))

make_button("−5s", lambda: shift_time(-5), control_bar).pack(side="left", padx=1)
make_button("−0.5s", lambda: shift_time(-0.5), control_bar).pack(side="left", padx=1)
make_button("Reset", reset_offset, control_bar).pack(side="left", padx=1)
make_button("+0.5s", lambda: shift_time(0.5), control_bar).pack(side="left", padx=1)
make_button("+5s", lambda: shift_time(5), control_bar).pack(side="left", padx=1)

opacity_scale = tk.Scale(
    control_bar, from_=30, to=100, orient="horizontal", showvalue=False, length=90,
    bg=THEME["bg"], fg=THEME["text"], troughcolor=THEME["button"], highlightthickness=0, bd=0,
    command=lambda v: root.attributes("-alpha", int(float(v)) / 100),
)
opacity_scale.set(settings.get("opacity", 93))
opacity_scale.pack(side="right")
tk.Label(control_bar, text="Opacity", bg=THEME["bg"], fg=THEME["info"],
         font=("Segoe UI", 9)).pack(side="right", padx=(10, 2))

info_label = tk.Label(root, text="", bg=THEME["bg"], fg=THEME["info"], font=("Segoe UI", 9),
                 justify="center")
info_label.pack(fill="x", before=search_bar)


def on_mousewheel(e):
    with lock:
        if e.delta > 0:
            state["scroll_offset"] = max(0, state["scroll_offset"] - 1)
        else:
            max_scroll = max(0, len(state["lyrics_lines"]) - 5)
            state["scroll_offset"] = min(max_scroll, state["scroll_offset"] + 1)


root.bind("<MouseWheel>", on_mousewheel)
for lb in lyric_labels:
    lb.bind("<MouseWheel>", on_mousewheel)
body.bind("<MouseWheel>", on_mousewheel)


def on_line_click(e, k):
    if abs(e.x_root - _drag.get("px", 0)) > 4 or abs(e.y_root - _drag.get("py", 0)) > 4:
        return
    with lock:
        times = state["lyrics_times"]
        if not times:
            return
        now = state["position"] + (time.monotonic() - state["pos_time"] if state["playing"] else 0)
        idx = bisect_right(times, now + state["offset"]) - 1 + state["scroll_offset"]
        j = idx + k - 2
        if 0 <= j < len(times):
            state["offset"] = times[j] + 0.3 - now
            state["scroll_offset"] = 0


for w in (top_bar, title_label, body, search_bar, control_bar, info_label):
    w.bind("<ButtonPress-1>", drag_start)
    w.bind("<B1-Motion>", drag_move)

for k, lb in enumerate(lyric_labels):
    lb.bind("<ButtonPress-1>", drag_start)
    lb.bind("<B1-Motion>", drag_move)
    lb.bind("<ButtonRelease-1>", lambda e, k=k: on_line_click(e, k))


def change_theme(new_theme):
    global theme_name, THEME
    theme_name = new_theme
    THEME = THEMES[new_theme]

    root.configure(bg=THEME["bg"])
    top_bar.configure(bg=THEME["top"])
    body.configure(bg=THEME["bg"])
    search_bar.configure(bg=THEME["bg"])
    control_bar.configure(bg=THEME["bg"])
    info_label.configure(bg=THEME["bg"], fg=THEME["info"])

    title_label.configure(bg=THEME["top"], fg=THEME["text"])
    close_btn.configure(bg=THEME["top"], fg=THEME["text"])
    hide_btn.configure(bg=THEME["top"], fg=THEME["text"])
    settings_btn.configure(bg=THEME["top"], fg=THEME["text"])

    search_entry.configure(bg=THEME["entry"], fg=THEME["text"], insertbackground=THEME["text"])
    opacity_scale.configure(bg=THEME["bg"], fg=THEME["text"], troughcolor=THEME["button"])

    for i, lb in enumerate(lyric_labels):
        is_center = i == 2
        lb.configure(bg=THEME["bg"], fg=ACTIVE_COLOR if is_center else THEME["passive"])

    for frame in (top_bar, body, search_bar, control_bar):
        for c in frame.winfo_children():
            if isinstance(c, tk.Button):
                c.configure(bg=THEME["button"] if frame != top_bar else THEME["top"],
                           fg=THEME["text"],
                           activebackground=THEME["button_active"], activeforeground=THEME["text"])

    for c in control_bar.winfo_children():
        if isinstance(c, tk.Label) and c != opacity_scale:
            c.configure(bg=THEME["bg"], fg=THEME["info"])


def change_color(new_color_name):
    global active_color_name, ACTIVE_COLOR
    active_color_name = new_color_name
    ACTIVE_COLOR = COLORS[new_color_name]["code"]
    lyric_labels[2].configure(fg=ACTIVE_COLOR)


def open_settings_menu():
    menu = tk.Menu(root, tearoff=0, bg=THEME["top"], fg=THEME["text"],
                   activebackground=THEME["button_active"], activeforeground=THEME["text"],
                   font=("Segoe UI", 9))

    # Theme options
    for t in THEMES:
        menu.add_command(label="🎨 " + t.capitalize(), command=lambda t=t: change_theme(t))

    menu.add_separator()

    # Active color options
    for c_name, c_info in COLORS.items():
        menu.add_command(label=c_info["name"], command=lambda c=c_name: change_color(c))

    x = settings_btn.winfo_rootx()
    y = settings_btn.winfo_rooty() + settings_btn.winfo_height()
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


settings_btn.configure(command=open_settings_menu)

# ---------- System tray ----------
tray_icon = None


def create_tray():
    global tray_icon
    if pystray is None:
        return
    img = Image.new("RGB", (64, 64), THEME["bg"])
    d = ImageDraw.Draw(img)
    d.ellipse((16, 40, 32, 56), fill=ACTIVE_COLOR)
    d.line((32, 48, 32, 16), fill=ACTIVE_COLOR, width=4)
    d.line((32, 16, 48, 20), fill=ACTIVE_COLOR, width=4)

    def show(icon, item):
        root.deiconify()
        root.attributes("-topmost", True)

    def hide(icon, item):
        root.withdraw()

    def quit_app(icon, item):
        icon.stop()
        close_app()

    menu = pystray.Menu(
        pystray.MenuItem("Show", show, default=True),
        pystray.MenuItem("Hide", hide),
        pystray.MenuItem("Exit", quit_app),
    )
    tray_icon = pystray.Icon("LyricsOverlay", img, "Lyrics Overlay", menu)
    threading.Thread(target=tray_icon.run, daemon=True).start()


create_tray()


def refresh():
    with lock:
        s = dict(state)
        times = state["lyrics_times"]
        lines = state["lyrics_lines"]
        scroll = state["scroll_offset"]
    if s["has_media"] and s["title"]:
        name = s["title"] if len(s["title"]) < 48 else s["title"][:45] + "..."
        title_label.config(text="♪ " + name)
    else:
        title_label.config(text="♪ Lyrics Overlay")

    if s["has_media"]:
        now = s["position"] + (time.monotonic() - s["pos_time"] if s["playing"] else 0)
        lyric_time = now + s["offset"]
        info = "Song: " + fmt_time(now) + " / " + fmt_time(s["duration"])
        info += "\nLyrics: " + fmt_time(lyric_time)
        if s["rec_duration"]:
            info += " / " + fmt_time(s["rec_duration"])
        info += "   (offset " + format(s["offset"], "+.1f") + " s)"
        if scroll != 0:
            info += "   [scroll: " + str(scroll) + " lines]"
        if times:
            k = bisect_right(times, lyric_time) - 1
            if k >= 0:
                info += "   line start: " + fmt_time(times[k])
        info_label.config(text=info)
    else:
        info_label.config(text="")

    if times:
        t = s["position"] + (time.monotonic() - s["pos_time"] if s["playing"] else 0) + s["offset"]
        idx = bisect_right(times, t) - 1 + scroll
        for k, lb in enumerate(lyric_labels):
            j = idx + k - 2
            if k == 2 and idx < 0:
                lb.config(text="♪")
            elif 0 <= j < len(lines):
                lb.config(text=lines[j] or "♪")
            else:
                lb.config(text="")
    else:
        for k, lb in enumerate(lyric_labels):
            lb.config(text=s["message"] if k == 2 else "")
    root.after(100, refresh)


refresh()
root.mainloop()