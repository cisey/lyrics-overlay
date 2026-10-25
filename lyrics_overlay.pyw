# -*- coding: utf-8 -*-
"""
Lyrics Overlay v1.6
Displays synced lyrics of the currently playing song (YouTube, Spotify, ...)
in a small always-on-top window on Windows.

Setup (once): pip install -r requirements.txt
Run: double-click lyrics_overlay.pyw
"""
import asyncio
import ctypes
import base64
import dataclasses
import html
import json
import os
import queue
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import datetime, timezone

try:
    from musicxmatch_api import MusixMatchAPI
    MUSIXMATCH_AVAILABLE = True
except Exception:
    MUSIXMATCH_AVAILABLE = False

try:   # optional: extra sources (Megalobiz, Genius) from the syncedlyrics package
    from syncedlyrics.providers import Megalobiz as _Megalobiz, Genius as _Genius
    EXTRA_AVAILABLE = True
except Exception:
    EXTRA_AVAILABLE = False

APP_VERSION = "1.6"
UA = "LyricsOverlay/" + APP_VERSION + " (https://github.com/cisey/lyrics-overlay)"
HOME = os.path.expanduser("~")
SETTINGS_FILE = os.path.join(HOME, ".lyrics_overlay_settings.json")
LOG_FILE = os.path.join(HOME, ".lyrics_overlay.log")
CACHE_FILE = os.path.join(HOME, ".lyrics_overlay_cache.json")
CACHE_MAX = 300             # remembered songs
MAX_ALTS = 12               # alternative results kept for the "Next" button

MAX_DURATION = 600          # seconds: longer videos are not treated as songs
DURATION_TOLERANCE = 25     # seconds: "same recording" window
MAX_DURATION_DIFF = 120     # seconds: still accepted (with a warning) up to this
MIN_SIM = 0.5               # minimum title similarity (0..1) to consider a result at all
STRONG_SIM = 0.75           # similarity needed to trust a result without further checks
SEARCH_DEADLINE = 10.0      # seconds to wait for the slower sources before using what we have
SCROLL_RETURN_SECONDS = 8   # after mouse-wheel browsing, go back to live line

THEMES = {
    "dark": {"label": "Dark", "bg": "#14141c", "top": "#1d1d29", "text": "#cfcfe0", "passive": "#7a7a90",
             "info": "#9a9ab5", "entry": "#25253a", "button": "#25253a", "button_active": "#3a3a55",
             "accent": "#ffd54a"},
    "amoled": {"label": "Pure black", "bg": "#000000", "top": "#0e0e0e", "text": "#d6d6d6", "passive": "#6e6e6e",
               "info": "#8c8c8c", "entry": "#181818", "button": "#181818", "button_active": "#2c2c2c",
               "accent": "#00e5a8"},
    "midnight": {"label": "Midnight purple", "bg": "#12101f", "top": "#1b1830", "text": "#d8d4f2",
                 "passive": "#7d77a8", "info": "#9b96c4", "entry": "#262244", "button": "#262244",
                 "button_active": "#3a3568", "accent": "#b388ff"},
    "blue": {"label": "Ocean blue", "bg": "#0a1929", "top": "#102a43", "text": "#b0c4de", "passive": "#607d8b",
             "info": "#78909c", "entry": "#1a3a5c", "button": "#1a3a5c", "button_active": "#2a5a8c",
             "accent": "#4fc3f7"},
    "forest": {"label": "Forest", "bg": "#0f1a14", "top": "#17271e", "text": "#d3e6d9", "passive": "#6f8f7c",
               "info": "#8fb09b", "entry": "#1f3328", "button": "#1f3328", "button_active": "#2f4d3d",
               "accent": "#7be08a"},
    "sunset": {"label": "Sunset", "bg": "#1f1216", "top": "#2c1a20", "text": "#f1d9d0", "passive": "#9a7378",
               "info": "#b89196", "entry": "#3a2229", "button": "#3a2229", "button_active": "#56323b",
               "accent": "#ff9a62"},
    "neon": {"label": "Neon", "bg": "#0b0b12", "top": "#14141f", "text": "#c8f7ff", "passive": "#4a5a7a",
             "info": "#6f86b0", "entry": "#1a1a2e", "button": "#1a1a2e", "button_active": "#2c2c4d",
             "accent": "#ff2bd6"},
    "light": {"label": "Light", "light": True, "bg": "#f0f0f0", "top": "#e0e0e0", "text": "#202020",
              "passive": "#808080", "info": "#606060", "entry": "#ffffff", "button": "#d0d0d0",
              "button_active": "#b0b0b0", "accent": "#c0392b"},
    "paper": {"label": "Paper", "light": True, "bg": "#f4ecd8", "top": "#e8dcc0", "text": "#3b3226",
              "passive": "#a39a86", "info": "#7d7462", "entry": "#fffaf0", "button": "#e0d3b3",
              "button_active": "#cfc09a", "accent": "#b5502b"},
}

# Active-line colors: (name shown in menu, color on dark themes, color on light themes)
# "auto" (None) = use the accent color of the current theme.
COLORS = {
    "auto": ("Theme accent", None, None),
    "yellow": ("Yellow", "#ffd54a", "#a67c00"),
    "orange": ("Orange", "#ff9f43", "#c85a00"),
    "green": ("Green", "#4cd137", "#1e8449"),
    "blue": ("Blue", "#4fc3f7", "#0b6fa4"),
    "pink": ("Pink", "#ff6b9d", "#c2185b"),
    "white": ("White", "#ffffff", "#000000"),
    "red": ("Red", "#e74c3c", "#b71c1c"),
}

FONTS = ["Segoe UI", "Calibri", "Georgia", "Verdana", "Trebuchet MS", "Consolas"]
SIZES = [("Small", 14), ("Medium", 17), ("Large", 21), ("Extra large", 26), ("Huge", 32)]


# ---------- Logging & settings ----------
def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 300000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


settings = load_settings()


def song_key(title, artist):
    return (title or "") + " | " + (artist or "")


def saved_offset(title, artist):
    try:
        return float(settings.get("offsets", {}).get(song_key(title, artist), 0.0))
    except Exception:
        return 0.0


def remember_offset(title, artist, value):
    """Remember the sync offset per song (most recent 500 songs)."""
    if not title:
        return
    offs = settings.setdefault("offsets", {})
    k = song_key(title, artist)
    offs.pop(k, None)
    if abs(value) >= 0.05:
        offs[k] = round(value, 1)
    while len(offs) > 500:
        offs.pop(next(iter(offs)))
    save_settings(settings)


# ---------- Shared state ----------
lock = threading.Lock()
state = {
    "title": "", "artist": "", "duration": 0.0, "position": 0.0,
    "pos_time": time.monotonic(), "playing": False, "has_media": False,
    "lyrics_times": [], "lyrics_lines": [], "message": "Waiting for music...",
    "token": 0, "key": None, "offset": 0.0, "rec_duration": 0,
    "scroll_offset": 0, "scroll_time": 0.0, "source": "", "note": "",
    "alts": [], "alt_idx": 0, "deep_done": False, "from_cache": False,
}


def fmt_time(x):
    x = max(0, int(x))
    return str(x // 60) + ":" + str(x % 60).zfill(2)


# ---------- Text helpers ----------
def norm_tokens(s):
    """Lowercase, strip accents (Turkish too), split into word tokens."""
    s = (s or "").replace("ı", "i").replace("İ", "I")
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"\w+", s)


def similarity(q_tokens, name, artist):
    """Fraction of query tokens found in the candidate's title+artist."""
    if not q_tokens:
        return 1.0
    pool = set(norm_tokens(name)) | set(norm_tokens(artist))
    if not pool:
        return 0.6
    return sum(1 for t in q_tokens if t in pool) / float(len(q_tokens))


JUNK_TOKENS = {"remastered", "remaster", "remastering", "live", "version", "edit", "radio", "original",
               "mix", "explicit", "feat", "ft", "official", "video", "audio", "lyrics", "lyric", "hd", "hq"}
BRACKET_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
INNER_RE = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]|\{([^}]*)\}")


def title_score(q_tokens, name):
    """How well a candidate's title matches the wanted title (0..1).
    Extra words in the candidate lower the score, so 'The Road to Glory' is not 'To Glory'."""
    if not q_tokens:
        return 1.0
    qs = set(q_tokens)
    variants = [BRACKET_RE.sub(" ", name or "")]
    variants += [next(g for g in m if g) if any(m) else "" for m in INNER_RE.findall(name or "")]
    variants.append(name or "")
    best = 0.0
    for v in variants:
        cs = set(t for t in norm_tokens(v) if t not in JUNK_TOKENS or t in qs)
        if cs:
            best = max(best, len(qs & cs) / float(max(len(qs), len(cs))))
    return best


def artist_ok(a_tokens, cand_artist, cand_name):
    """False when we know both artists and they clearly differ."""
    if not a_tokens or not norm_tokens(cand_artist):
        return True
    pool = set(norm_tokens(cand_artist)) | set(norm_tokens(cand_name))
    return sum(1 for t in a_tokens if t in pool) / float(len(a_tokens)) >= 0.5


def pair_sim(a_tokens, t_tokens, name, artist):
    sc = title_score(t_tokens, name)
    if not artist_ok(a_tokens, artist, name):
        sc *= 0.5
    return sc


JUNK_RE = re.compile(
    r"\b(official\s+(music\s+)?video|official\s+audio|official\s+lyric\s+video|lyrics?\s+video|"
    r"video\s+clip|resmi\s+klip|şarkı\s+sözleri|sarki\s+sozleri|sözleri|sozleri|lyrics?|audio|"
    r"visuali[sz]er|klip|4k|hd|hq)\b",
    re.I,
)


def clean_title(title):
    t = (title or "").replace("｜", "|").replace("•", " ")
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", t)
    t = t.split("|")[0]
    t = re.sub(r"\s+[–—]\s+", " - ", t)
    t = JUNK_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" -–—:")


def clean_artist(a):
    a = re.sub(r"\s*-\s*Topic$", "", a or "", flags=re.I)
    a = re.sub(r"VEVO$", "", a, flags=re.I)
    return a.strip()


def strip_feat(s):
    return re.sub(r"\s*\b(feat|ft)\b\.?.*$", "", s, flags=re.I).strip()


def guess_pairs(title, artist):
    """Possible (artist, track) readings of a messy media title."""
    t = clean_title(title)
    a = clean_artist(artist)
    pairs = []
    if " - " in t:
        left, right = [x.strip() for x in t.split(" - ", 1)]
        pairs += [(left, right), (right, left)]
        if a:
            pairs.append((a, left))
    if t:
        pairs.append((a, t))
    out, seen, same = [], set(), []
    for x, y in pairs:
        for yy in (y, strip_feat(y)):
            if not yy:
                continue
            key = (x.lower(), yy.lower())
            if key in seen:
                continue
            seen.add(key)
            (same if yy.lower() == x.lower() else out).append((x, yy))   # artist == title is rarely right
    return (out or same)[:6]


def make_queries(pairs):
    qs, low = [], set()
    for a, t in pairs:
        for q in ((a + " " + t).strip(), t):
            if q and q.lower() not in low:
                low.add(q.lower())
                qs.append(q)
    return qs[:6]


# ---------- Lyrics parsing ----------
CREDIT_ZH = re.compile(r"^\s*(作词|作曲|编曲|制作人|制作|监制|词|曲|混音|母带|录音|和声|吉他|贝斯|鼓|弦乐|演唱|原唱|"
                       r"翻唱|出品|发行|策划|统筹|封面|OP|SP|ISRC)\s*[:：]")
CREDIT_EN = re.compile(r"^\s*(lyrics?|words?|music|composed|composer|written|writers?|arranged|arranger|"
                       r"producer|produced|mixed|mixing|mastered|mastering|performed|vocals?|guitars?|bass|"
                       r"drums|keyboards?|publisher|label|artist|album|title|song)(\s+by)?\s*[:：]", re.I)


def is_credit(content, position):
    """Credit lines (composer, producer, ...) are not lyrics. English ones only count near the top."""
    return bool(CREDIT_ZH.match(content) or (position < 10 and CREDIT_EN.match(content)))


def parse_lrc(text):
    times, lines = [], []
    for line in (text or "").splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        content = re.sub(r"\[[^\]]*\]", "", line).strip()
        if not tags:
            continue
        for m, sec in tags:
            times.append(int(m) * 60 + float(sec))
            lines.append(content)
    pairs = sorted(zip(times, lines), key=lambda p: p[0])
    kept = [p for i, p in enumerate(pairs) if not is_credit(p[1], i)]
    return [p[0] for p in kept], [p[1] for p in kept]


def parse_any_synced(text):
    """LRC text, or Musixmatch-style JSON [{"text":..,"time":{"total":..}}]."""
    text = text or ""
    s = text.lstrip()
    if s.startswith("[") and '"text"' in s[:400]:
        try:
            arr = json.loads(s)
            times, lines = [], []
            for it in arr:
                tt = it.get("time", {})
                sec = tt.get("total") if isinstance(tt, dict) else tt
                times.append(float(sec))
                lines.append(str(it.get("text", "")).strip())
            if times:
                return times, lines
        except Exception:
            pass
    return parse_lrc(text)


def clean_plain(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or re.match(r"^\[[^\]]*\]$", line):
            continue
        out.append(line)
    return out


def estimate_times(n, duration):
    """Rough timing for unsynced lyrics: spread the lines over the song."""
    if n <= 0:
        return []
    if duration and duration > 30:
        lead = min(12.0, duration * 0.05)
        tail = min(10.0, duration * 0.05)
        step = max(duration - lead - tail, float(n)) / n
        return [lead + i * step for i in range(n)]
    return [4.0 * i for i in range(n)]


# ---------- Candidates ----------
@dataclasses.dataclass
class Cand:
    source: str
    name: str
    artist: str
    duration: float
    sim: float
    times: list
    lines: list
    plain_lines: list
    verified: bool = True   # False: title/artist could not be checked against the song


def make_cand(source, name, artist, duration, sim, lrc="", plain="", verified=True):
    times, lines = parse_any_synced(lrc) if lrc else ([], [])
    if len(times) < 3:
        times, lines = [], []
    plain_lines = clean_plain(plain)
    if len(plain_lines) < 4:
        plain_lines = []
    if not times and not plain_lines:
        return None
    return Cand(source, name or "", artist or "", float(duration or 0), sim, times, lines, plain_lines,
                verified)


def duration_diff(c, duration):
    if duration > 0 and c.duration:
        return abs(c.duration - duration)
    return DURATION_TOLERANCE / 2.0   # unknown: neutral


def is_exact(c, duration, manual):
    if not c.times or not c.verified:
        return False
    if manual:
        return c.sim >= MIN_SIM
    return c.sim >= STRONG_SIM and duration_diff(c, duration) <= DURATION_TOLERANCE


def plausible_unverified(c, duration):
    """Unverified synced text: accept only if its last line fits inside the video."""
    if not c.times:
        return False
    return duration <= 0 or c.times[-1] <= duration + 30


def choose(cands, duration, manual):
    """Best verified synced; else close verified mismatch; else unverified synced;
    else unsynced text (verified first)."""
    synced = [c for c in cands if c.times and c.verified]
    if synced:
        synced.sort(key=lambda c: (0 if is_exact(c, duration, manual) else 1,
                                   -round(c.sim, 1), duration_diff(c, duration)))
        best = synced[0]
        if is_exact(best, duration, manual):
            return best, ""
        if best.sim >= (MIN_SIM if manual else STRONG_SIM) and duration_diff(best, duration) <= MAX_DURATION_DIFF:
            return best, ("record " + fmt_time(best.duration) + " vs video " + fmt_time(duration)
                          + " - click a line to sync")
    loose = [c for c in cands if c.times and (
        (not c.verified and plausible_unverified(c, duration)) or
        (c.verified and c.sim < STRONG_SIM and duration_diff(c, duration) <= DURATION_TOLERANCE))]
    if loose:
        return loose[0], "match not verified - may be wrong, click a line to sync"
    plain = [c for c in cands if c.plain_lines and (
        not c.verified or c.sim >= (MIN_SIM if manual else STRONG_SIM))]
    if plain:
        plain.sort(key=lambda c: (0 if c.verified else 1, -round(c.sim, 1), duration_diff(c, duration)))
        note = "unsynced text, estimated timing"
        if not plain[0].verified:
            note += " (match not verified)"
        return plain[0], note
    return None


# ---------- Sources ----------
def http_json(url, headers=None, data=None, timeout=8):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace").lstrip("\ufeff").strip()
    if text and text[0] not in "{[":
        m = re.match(r"^[\w.$]+\((.*)\);?\s*$", text, re.S)   # callback({...})
        if m:
            text = m.group(1)
    return json.loads(text)


def best_sim(q_list, name, artist):
    """q_list items: token list (manual search: all words) or (artist_tokens, title_tokens) tuple."""
    best = 0.0
    for q in q_list:
        v = pair_sim(q[0], q[1], name, artist) if isinstance(q, tuple) else similarity(q, name, artist)
        best = max(best, v)
    return best


def src_lrclib(pairs, queries, qtl, duration, manual):
    urls = []
    if not manual:
        for a, t in pairs[:3]:
            if a:
                urls.append("https://lrclib.net/api/search?" +
                            urllib.parse.urlencode({"track_name": t, "artist_name": a}))
    for q in queries[:4]:
        urls.append("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": q}))
    cands, seen, ok, last = [], set(), 0, None
    for url in urls:
        try:
            results = http_json(url)
            ok += 1
        except Exception as e:
            last = e
            continue
        for x in results or []:
            rid = x.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            if x.get("instrumental"):
                continue
            name, artist = x.get("trackName", ""), x.get("artistName", "")
            sim = best_sim(qtl, name, artist)
            if sim < MIN_SIM:
                continue
            c = make_cand("LRCLIB", name, artist, x.get("duration"), sim,
                          x.get("syncedLyrics") or "", x.get("plainLyrics") or "")
            if c:
                cands.append(c)
        if any(is_exact(c, duration, manual) for c in cands):
            break
    if ok == 0 and last:
        raise last
    return cands


NETEASE_HEADERS = {"Referer": "https://music.163.com/",
                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _d(x):
    """Always a dict: unexpected server answers (lists, strings, null) become {}."""
    return x if isinstance(x, dict) else {}


def _dicts(x):
    return [i for i in (x if isinstance(x, list) else []) if isinstance(i, dict)]


def _artist_names(x):
    out = []
    for a in (x if isinstance(x, list) else []):
        out.append(a.get("name", "") if isinstance(a, dict) else str(a))
    return " ".join(out)


def src_netease(queries, qtl, duration, manual):
    cands, ok, last = [], 0, None
    for q in queries[:3]:
        try:
            body = urllib.parse.urlencode({"s": q, "type": 1, "limit": 6, "offset": 0}).encode()
            data = http_json("https://music.163.com/api/search/get/web?csrf_token=",
                             headers=NETEASE_HEADERS, data=body)
            ok += 1
        except Exception as e:
            last = e
            continue
        result = data.get("result") if isinstance(data, dict) else None
        songs = _dicts(result.get("songs")) if isinstance(result, dict) else []
        if not songs:
            log("  NetEase '%s': no songs (response: %s)" % (q, str(data)[:100]))
        scored = []
        for sg in songs:
            name = str(sg.get("name") or "")
            artists = _artist_names(sg.get("artists") or sg.get("ar"))
            try:
                dur = float(sg.get("duration") or sg.get("dt") or 0) / 1000.0
            except Exception:
                dur = 0.0
            sim = best_sim(qtl, name, artists)
            if sim >= MIN_SIM and sg.get("id"):
                diff = abs(dur - duration) if (duration > 0 and dur) else 999
                scored.append((-round(sim, 1), diff, sg, name, artists, dur, sim))
        scored.sort(key=lambda x: (x[0], x[1]))
        for _, _, sg, name, artists, dur, sim in scored[:2]:
            try:
                ly = http_json("https://music.163.com/api/song/lyric?id=%s&lv=1&kv=1&tv=-1" % sg["id"],
                               headers=NETEASE_HEADERS)
            except Exception as e:
                last = e
                continue
            lrc_obj = ly.get("lrc") if isinstance(ly, dict) else None
            lrc = (lrc_obj.get("lyric") if isinstance(lrc_obj, dict) else "") or ""
            if "[" not in lrc or "纯音乐" in lrc:
                continue
            c = make_cand("NetEase", name, artists, dur, sim, lrc, "")
            if c:
                cands.append(c)
        if any(is_exact(c, duration, manual) for c in cands):
            break
    if ok == 0 and last:
        raise last
    return cands


KUGOU_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _kugou_get(host, path_qs):
    last = None
    for scheme in ("https", "http"):
        try:
            return http_json(scheme + "://" + host + path_qs, headers=KUGOU_HEADERS)
        except Exception as e:
            last = e
    raise last


def _kugou_lrc(song_hash, keyword, duration):
    q = urllib.parse.urlencode({"ver": 1, "man": "yes", "client": "pc", "keyword": keyword,
                                "duration": int(duration * 1000), "hash": song_hash})
    found = _d(_kugou_get("lyrics.kugou.com", "/search?" + q))
    for cand in _dicts(found.get("candidates"))[:2]:
        d = urllib.parse.urlencode({"ver": 1, "client": "pc", "id": cand.get("id"),
                                    "accesskey": cand.get("accesskey"), "fmt": "lrc", "charset": "utf8"})
        data = _d(_kugou_get("lyrics.kugou.com", "/download?" + d))
        content = data.get("content") or ""
        if not isinstance(content, str):
            continue
        if not content:
            continue
        try:
            text = base64.b64decode(content).decode("utf-8", "replace").lstrip("\ufeff")
        except Exception:
            continue
        if "[" in text:
            return text
    return ""


def src_kugou(queries, qtl, duration, manual):
    cands, ok, last = [], 0, None
    for q in queries[:3]:
        try:
            data = _kugou_get("mobilecdn.kugou.com", "/api/v3/search/song?" + urllib.parse.urlencode(
                {"format": "json", "keyword": q, "page": 1, "pagesize": 12, "showtype": 1}))
            ok += 1
        except Exception as e:
            last = e
            continue
        scored = []
        items = []
        for it in _dicts(_d(_d(data).get("data")).get("info")):
            items.append(it)
            items.extend(_dicts(it.get("group")))   # other versions of the same song
        log("  KuGou '%s': %d songs" % (q, len(items)))
        for it in items:
            name, artist = str(it.get("songname") or ""), str(it.get("singername") or "")
            try:
                dur = float(it.get("duration") or 0)
            except Exception:
                dur = 0.0
            sim = best_sim(qtl, name, artist)
            if sim >= MIN_SIM and it.get("hash"):
                diff = abs(dur - duration) if (duration > 0 and dur) else 999
                scored.append((-round(sim, 1), diff, it, name, artist, dur, sim))
        scored.sort(key=lambda x: (x[0], x[1]))
        log("  KuGou '%s': %d pass the title/artist check" % (q, len(scored)))
        for _, _, it, name, artist, dur, sim in scored[:3]:
            try:
                lrc = _kugou_lrc(it["hash"], q, dur or duration)
            except Exception as e:
                last = e
                continue
            if not lrc:
                log("  KuGou: no lyrics for %r / %r" % (name, artist))
            c = make_cand("KuGou", name, artist, dur, sim, lrc, "")
            if c:
                cands.append(c)
        if any(is_exact(c, duration, manual) for c in cands):
            break
    if ok == 0 and last:
        raise last
    return cands


QQ_HEADERS = {"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _qq_lyric(mid):
    d = http_json("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?" + urllib.parse.urlencode(
        {"songmid": mid, "format": "json", "nobase64": 1, "g_tk": 5381}), headers=QQ_HEADERS)
    text = _d(d).get("lyric") or ""
    if not isinstance(text, str):
        return ""
    if text and "[" not in text:
        try:
            text = base64.b64decode(text).decode("utf-8", "replace")
        except Exception:
            pass
    return html.unescape(text)


def src_qqmusic(queries, qtl, duration, manual):
    cands, ok, last = [], 0, None
    for q in queries[:3]:
        try:
            data = http_json("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?" + urllib.parse.urlencode(
                {"w": q, "format": "json", "p": 1, "n": 6, "new_json": 1}), headers=QQ_HEADERS)
            ok += 1
        except Exception as e:
            last = e
            continue
        scored = []
        qq_list = _dicts(_d(_d(_d(data).get("data")).get("song")).get("list"))
        log("  QQ Music '%s': %d songs" % (q, len(qq_list)))
        for sg in qq_list:
            name = str(sg.get("name") or sg.get("songname") or "")
            artist = _artist_names(sg.get("singer"))
            mid = sg.get("mid") or sg.get("songmid")
            try:
                dur = float(sg.get("interval") or 0)
            except Exception:
                dur = 0.0
            sim = best_sim(qtl, name, artist)
            if sim >= MIN_SIM and mid:
                diff = abs(dur - duration) if (duration > 0 and dur) else 999
                scored.append((-round(sim, 1), diff, mid, name, artist, dur, sim))
        scored.sort(key=lambda x: (x[0], x[1]))
        for _, _, mid, name, artist, dur, sim in scored[:2]:
            try:
                lrc = _qq_lyric(mid)
            except Exception as e:
                last = e
                continue
            c = make_cand("QQ Music", name, artist, dur, sim, lrc, "")
            if c:
                cands.append(c)
        if any(is_exact(c, duration, manual) for c in cands):
            break
    if ok == 0 and last:
        raise last
    return cands


_mxm = None
_mxm_failures = 0     # after 3 failures in a row the unofficial library is skipped for this session


def _mxm_tracks(results):
    if isinstance(results, dict):
        body = results.get("message", {}).get("body", results)
        lst = body.get("track_list") if isinstance(body, dict) else None
        results = lst if lst is not None else (results.get("track_list") or results.get("tracks") or [])
    out = []
    for r in results or []:
        if isinstance(r, dict) and isinstance(r.get("track"), dict):
            r = r["track"]
        if isinstance(r, dict):
            out.append(r)
    return out


def _mxm_body(sub):
    if isinstance(sub, str):
        return sub
    if isinstance(sub, dict):
        if sub.get("subtitle_body"):
            return sub["subtitle_body"]
        try:
            return sub["message"]["body"]["subtitle"]["subtitle_body"]
        except Exception:
            return ""
    return ""


def src_musixmatch(queries, qtl, duration, manual):
    """Best effort: unofficial library, never raises."""
    global _mxm, _mxm_failures
    cands = []
    if not MUSIXMATCH_AVAILABLE or _mxm_failures >= 3:
        return cands
    try:
        if _mxm is None:
            _mxm = MusixMatchAPI()
        for q in queries[:2]:
            tracks = _mxm_tracks(_mxm.search_tracks(q))
            log("Musixmatch '" + q + "': " + str(len(tracks)) + " tracks")
            for tr in tracks[:3]:
                tid = tr.get("track_id")
                if not tid:
                    continue
                name = tr.get("track_name") or tr.get("name") or ""
                artist = tr.get("artist_name") or ""
                sim = best_sim(qtl, name, artist) if (name or artist) else 0.55
                if sim < MIN_SIM:
                    continue
                body = _mxm_body(_mxm.get_subtitle(tid))
                c = make_cand("Musixmatch", name, artist, tr.get("track_length"), sim, body, "")
                if c:
                    cands.append(c)
            if any(is_exact(c, duration, manual) for c in cands):
                break
        _mxm_failures = 0
    except Exception as e:
        _mxm_failures += 1
        log("Musixmatch failed (%d/3): %s: %s" % (_mxm_failures, type(e).__name__, str(e)[:100]))
    return cands


def src_extra(queries, duration, manual):
    """Megalobiz (synced) and Genius (plain). Their results carry no title/duration,
    so they are marked unverified and only used when nothing better exists."""
    cands, last, ok = [], None, 0
    if not EXTRA_AVAILABLE:
        return cands
    for provider in (_Megalobiz, _Genius):
        pname = provider.__name__.lstrip("_")
        got = False
        for q in queries[:2]:
            try:
                lyr = provider().get_lrc(q)
                ok += 1
            except Exception as e:
                last = e
                log(pname + " failed: " + type(e).__name__ + ": " + str(e)[:100])
                continue
            if not lyr:
                continue
            c = make_cand(pname, "", "", 0, MIN_SIM, getattr(lyr, "synced", "") or "",
                          getattr(lyr, "unsynced", "") or "", verified=False)
            if c:
                cands.append(c)
                got = True
                break
        log("  extra %s: %s" % (pname, "found" if got else "nothing"))
    if ok == 0 and last:
        raise last
    return cands


def run_source(name, fn, cands, errors, counts):
    try:
        res = fn()
        counts[name] = len(res)
        cands.extend(res)
    except Exception as e:
        errors[name] = (type(e).__name__ + ": " + str(e))[:100]


# ---------- Remembering results ----------
_cache = {"data": None}
cache_lock = threading.Lock()


def _cache_load():
    if _cache["data"] is None:
        data = {}
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        _cache["data"] = data if isinstance(data, dict) else {}
    return _cache["data"]


def cache_get(title, artist, duration):
    with cache_lock:
        e = _cache_load().get(song_key(title, artist))
    if not isinstance(e, dict):
        return None
    try:
        c = Cand(e["source"], e["name"], e["artist"], float(e["duration"]), float(e["sim"]),
                 [float(x) for x in e["times"]], list(e["lines"]), list(e["plain_lines"]), bool(e["verified"]))
    except Exception:
        return None
    if not (c.times or c.plain_lines):
        return None
    if duration > 0 and c.duration and abs(c.duration - duration) > 60:
        return None   # same title, clearly another recording
    return c


def cache_put(title, artist, c):
    if not title or c is None:
        return
    entry = {"source": c.source, "name": c.name, "artist": c.artist, "duration": c.duration, "sim": c.sim,
             "times": [round(x, 2) for x in c.times], "lines": c.lines, "plain_lines": c.plain_lines,
             "verified": c.verified}
    with cache_lock:
        data = _cache_load()
        k = song_key(title, artist)
        data.pop(k, None)
        data[k] = entry
        while len(data) > CACHE_MAX:
            data.pop(next(iter(data)))
        try:
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            log("cache write failed: " + repr(e))


def cache_clear():
    with cache_lock:
        _cache["data"] = {}
        try:
            if os.path.exists(CACHE_FILE):
                os.remove(CACHE_FILE)
        except Exception:
            pass


# ---------- Ranking alternatives ----------
def describe(c, duration, manual):
    """Short warning text for a candidate ('' = trusted)."""
    if c.times:
        if c.verified and is_exact(c, duration, manual):
            return ""
        weak = (not c.verified) or (not manual and c.sim < STRONG_SIM)
        if weak:
            return "match not verified - may be wrong, click a line to sync"
        if duration_diff(c, duration) <= MAX_DURATION_DIFF:
            return "record " + fmt_time(c.duration) + " vs video " + fmt_time(duration) + " - click a line to sync"
        return "different length (record " + fmt_time(c.duration) + " vs video " + fmt_time(duration) + ") - may not match"
    note = "unsynced text, estimated timing"
    return note + ("" if c.verified else " (match not verified)")


def rank_key(c, duration, manual):
    if c.times:
        if c.verified and is_exact(c, duration, manual):
            cls = 0
        elif c.verified and c.sim >= (MIN_SIM if manual else STRONG_SIM) and duration_diff(c, duration) <= MAX_DURATION_DIFF:
            cls = 1
        elif not c.verified or c.sim < STRONG_SIM:
            cls = 2
        else:
            cls = 3
    else:
        cls = 4 if c.verified else 5
    return (cls, -round(c.sim, 1), duration_diff(c, duration))


def cand_id(c):
    return (c.source, c.name, c.artist, int(c.duration), len(c.times), len(c.plain_lines),
            (c.lines or c.plain_lines or [""])[0])


def build_alts(first, cands, duration, manual):
    """[(candidate, note), ...] with the chosen result first, then the others best-first."""
    ordered = sorted(cands, key=lambda c: rank_key(c, duration, manual))
    out, seen = [], set()
    for c in ([first] if first else []) + ordered:
        if c is None or cand_id(c) in seen:
            continue
        seen.add(cand_id(c))
        out.append((c, describe(c, duration, manual)))
    return out[:MAX_ALTS]


def set_lyrics(c, note, duration):
    """Show a candidate. The caller must hold `lock`."""
    if c.times:
        times, lines = c.times, c.lines
    else:
        lines = c.plain_lines
        times = estimate_times(len(lines), duration if duration > 0 else c.duration)
    state["lyrics_times"], state["lyrics_lines"] = times, lines
    state["message"] = ""
    state["rec_duration"] = int(c.duration)
    state["source"] = c.source
    state["note"] = note


# ---------- Searching ----------
def search_all(title, artist, duration, manual, deep=False):
    """Ask all services. Returns (candidates, errors, counts, first query).
    Normal mode stops as soon as a trusted result exists; deep mode waits for everybody."""
    cands, errors, counts = [], {}, {}
    q0 = manual or ""
    if manual:
        words = manual.split()
        queries = [manual] + [" ".join(words[k:]) for k in range(1, min(3, len(words) - 1) + 1)]
        pairs = []
        qtl = [norm_tokens(manual)]
    else:
        pairs = guess_pairs(title, artist)
        queries = make_queries(pairs)
        qtl = [(norm_tokens(a), norm_tokens(t)) for a, t in pairs] or [[]]
        q0 = queries[0] if queries else title
    log("%s title=%r artist=%r dur=%s manual=%r" % ("DEEP SEARCH" if deep else "SEARCH", title, artist,
                                                    int(duration), manual))
    log("  pairs=%r queries=%r" % (pairs, queries))
    run_source("LRCLIB", lambda: src_lrclib(pairs, queries, qtl, duration, manual), cands, errors, counts)
    if deep or not any(is_exact(c, duration, manual) for c in cands):
        others = [("NetEase", lambda: src_netease(queries, qtl, duration, manual)),
                  ("KuGou", lambda: src_kugou(queries, qtl, duration, manual)),
                  ("QQ Music", lambda: src_qqmusic(queries, qtl, duration, manual))]
        if MUSIXMATCH_AVAILABLE:
            others.append(("Musixmatch", lambda: src_musixmatch(queries, qtl, duration, manual)))
        workers = [threading.Thread(target=run_source, args=(n, f, cands, errors, counts), daemon=True)
                   for n, f in others]
        for w in workers:
            w.start()
        deadline = time.time() + SEARCH_DEADLINE
        while time.time() < deadline and any(w.is_alive() for w in workers):
            if not deep and any(is_exact(c, duration, manual) for c in list(cands)):
                break   # good enough - do not wait for the slower services
            time.sleep(0.05)
    if EXTRA_AVAILABLE and (deep or not any(is_exact(c, duration, manual) for c in cands)):
        run_source("Extra", lambda: src_extra(queries, duration, manual), cands, errors, counts)
    return list(cands), dict(errors), dict(counts), q0   # late answers from slow services are ignored


def failure_message(cands, errors, q0):
    n_sources = 4 + (1 if MUSIXMATCH_AVAILABLE else 0) + (1 if EXTRA_AVAILABLE else 0)
    # "no internet" only if the main service failed too; otherwise some services are just blocked
    if ("LRCLIB" in errors and len(errors) >= 2) or len(errors) >= n_sources:
        msg = "Could not reach the lyrics services (check your internet)"
    elif not cands:
        msg = "No lyrics found: «" + q0 + "»"
    else:
        msg = "Found tracks but no usable lyrics: «" + q0 + "»"
    if errors and "reach" not in msg:
        msg += "\n(" + ", ".join(sorted(errors)) + " unreachable)"
    return msg


def fetch_lyrics(token, title, artist, duration, manual):
    t0 = time.time()
    cands, errors, counts, q0 = [], {}, {}, manual or title
    choice = None
    try:
        cands, errors, counts, q0 = search_all(title, artist, duration, manual)
        choice = choose(cands, duration, manual)
    except Exception as e:
        errors["internal"] = (type(e).__name__ + ": " + str(e))[:100]

    log("  counts=%r errors=%r took=%.1fs" % (counts, errors, time.time() - t0))
    if choice:
        c, note = choice
        log("  CHOSEN %s: %r / %r dur=%s sim=%.2f synced=%s note=%s" % (
            c.source, c.name, c.artist, int(c.duration), c.sim, bool(c.times), note))
    else:
        log("  NOTHING CHOSEN (candidates=%d)" % len(cands))

    alts = build_alts(choice[0] if choice else None, cands, duration, manual) if cands else []
    with lock:
        if state["token"] != token:
            return
        state["alts"], state["alt_idx"], state["deep_done"], state["from_cache"] = alts, 0, False, False
        if choice:
            set_lyrics(choice[0], choice[1], duration)
        else:
            state["message"] = failure_message(cands, errors, q0)
    if choice and choice[0].times and not choice[1]:
        cache_put(title, artist, choice[0])   # trusted result: remember it for next time


def deep_search(token, title, artist, duration, manual):
    """'Next' pressed after the quick results ran out: ask every service and add the new results."""
    cands, errors, counts, q0 = [], {}, {}, ""
    try:
        cands, errors, counts, q0 = search_all(title, artist, duration, manual, deep=True)
    except Exception as e:
        errors["internal"] = (type(e).__name__ + ": " + str(e))[:100]
    log("  deep counts=%r errors=%r" % (counts, errors))
    with lock:
        if state["token"] != token:
            return
        known = [a for a, _ in state["alts"]]
        merged = build_alts(known[0] if known else None, known + cands, duration, manual)
        old_ids = set(cand_id(c) for c in known)
        state["alts"] = merged
        state["deep_done"] = True
        nxt = state["alt_idx"] + 1
        fresh = [i for i, (c, _) in enumerate(merged) if cand_id(c) not in old_ids]
        if fresh:
            state["alt_idx"] = fresh[0]
            set_lyrics(merged[fresh[0]][0], merged[fresh[0]][1], duration)
            state["offset"] = 0.0
            state["from_cache"] = False
        else:
            state["note"] = "no other results found"
    with lock:
        picked = state["alts"][state["alt_idx"]][0] if state["alts"] else None
    if picked is not None and not state["note"].startswith("no other"):
        cache_put(title, artist, picked)


def next_result():
    """Switch to the next alternative; asks the other services once the list is used up."""
    with lock:
        t, a, d, token = state["title"], state["artist"], state["duration"], state["token"]
        alts, idx, deep_done = state["alts"], state["alt_idx"], state["deep_done"]
        if not t:
            return
        if idx + 1 < len(alts):
            state["alt_idx"] = idx + 1
            c, note = alts[idx + 1]
            set_lyrics(c, note, d)
            state["offset"] = 0.0
            state["scroll_offset"] = 0
            state["from_cache"] = False
            picked = c
        elif not deep_done:
            state["message"] = "Searching more sources..." if not state["lyrics_times"] else state["message"]
            state["note"] = "searching more sources..."
            picked = None
        else:
            state["note"] = "no other results found"
            return
    if picked is not None:
        cache_put(t, a, picked)
    else:
        threading.Thread(target=deep_search, args=(token, t, a, d, None), daemon=True).start()


def new_song(title, artist, duration, manual=None, force=False):
    with lock:
        state["token"] += 1
        token = state["token"]
        state["lyrics_times"], state["lyrics_lines"] = [], []
        state["offset"] = saved_offset(title, artist)
        state["rec_duration"] = 0
        state["scroll_offset"] = 0
        state["source"] = ""
        state["note"] = ""
        state["alts"], state["alt_idx"], state["deep_done"], state["from_cache"] = [], 0, False, False
        if manual is None and duration > MAX_DURATION:
            state["message"] = "Doesn't look like a song (video too long)"
            return
        if manual is None and not clean_title(title):
            state["message"] = "Waiting for music..."
            return
        if manual is None and not force:
            c = cache_get(title, artist, duration)
            if c:
                set_lyrics(c, "", duration)
                state["source"] = "Saved (" + c.source + ")"
                state["alts"], state["from_cache"] = [(c, "")], True
                log("CACHE HIT title=%r artist=%r from %s" % (title, artist, c.source))
                return
        state["message"] = "Searching lyrics..."
    threading.Thread(target=fetch_lyrics, args=(token, title, artist, duration, manual),
                     daemon=True).start()


def song_clock(s):
    return s["position"] + ((time.monotonic() - s["pos_time"]) if s["playing"] else 0.0)


# ====== UI ======  (everything below needs Windows + tkinter)
import tkinter as tk
import tkinter.font as tkfont
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
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "Missing package",
        "The 'winsdk' package is not installed.\n\nOpen a terminal and run:\n\npip install winsdk",
    )
    raise SystemExit

# ---------- Appearance settings ----------
theme_name = settings.get("theme", "dark")
if theme_name not in THEMES:
    theme_name = "dark"
THEME = THEMES[theme_name]

active_color_name = settings.get("active_color", "yellow")
if active_color_name not in COLORS:
    active_color_name = "yellow"

font_family = settings.get("font", FONTS[0])
if font_family not in FONTS:
    font_family = FONTS[0]

try:
    font_size = max(12, min(34, int(settings.get("font_size", 17))))
except Exception:
    font_size = 17

transparent = bool(settings.get("transparent", False))
autohide = bool(settings.get("autohide", True))
snap_enabled = bool(settings.get("snap", True))
SNAP_DISTANCE = 16       # pixels: how close to an edge before the window snaps to it

KEY = "#010203"          # color that becomes see-through in transparent mode
HIDE_DELAY = 1.2         # seconds before the controls hide after the mouse leaves

ui_queue = queue.Queue()   # actions requested from other threads (tray menu)
tray_icon = None
menu_open = False


def is_light():
    return bool(THEME.get("light"))


def active_color():
    name, dark, light = COLORS[active_color_name]
    if dark is None:
        return THEME["accent"]
    return light if is_light() else dark


def mix(c1, c2, f):
    """Blend two #rrggbb colors; f=0 gives c1, f=1 gives c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * f)) for i in range(3))


def canvas_bg():
    return KEY if transparent else THEME["bg"]


def outline_color():
    return "#ffffff" if is_light() else "#000000"


def line_color(dist):
    """dist 0 = current line, 1 and 2 = lines further away (they fade)."""
    if dist == 0:
        return active_color()
    if transparent:
        return mix(outline_color(), THEME["text"], 0.85 if dist == 1 else 0.6)
    return mix(THEME["bg"], THEME["passive"], 1.0 if dist == 1 else 0.6)


# ---------- Windows media polling ----------
async def poll_loop():
    try:
        mgr = await MediaManager.request_async()
    except Exception as e:
        log("Windows media API unavailable: " + repr(e))
        return
    last_err = ""
    while True:
        try:
            sess = mgr.get_current_session()
            if sess is None:
                with lock:
                    state["has_media"] = False
                    state["playing"] = False
                    if state["key"] is not None:
                        state["key"] = None
                        state["token"] += 1
                        state["message"] = "Waiting for music..."
                        state["lyrics_times"], state["lyrics_lines"] = [], []
                        state["source"] = ""
                        state["note"] = ""
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
                        changed = True
                if changed and title:
                    new_song(title, artist, dur)
        except Exception as e:
            if repr(e) != last_err:
                last_err = repr(e)
                log("poll error: " + last_err)
        await asyncio.sleep(0.5)


threading.Thread(target=lambda: asyncio.run(poll_loop()), daemon=True).start()

# ---------- Window ----------
root = tk.Tk()
root.title("Lyrics Overlay")
root.overrideredirect(True)
root.attributes("-topmost", True)
root.attributes("-alpha", settings.get("opacity", 93) / 100)
root.configure(bg=canvas_bg())

try:
    WINDOW_W = max(300, int(settings.get("width", 480)))
    WINDOW_H = max(220, int(settings.get("height", 340)))
except Exception:
    WINDOW_W, WINDOW_H = 480, 340


def virtual_screen():
    try:
        u = ctypes.windll.user32
        return u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    except Exception:
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


_vx, _vy, _vw, _vh = virtual_screen()
_x = settings.get("x", _vx + _vw - WINDOW_W - 20)
_y = settings.get("y", _vy + 60)
try:
    _x = int(min(max(_x, _vx), _vx + _vw - 120))   # keep the window reachable
    _y = int(min(max(_y, _vy), _vy + _vh - 80))
except Exception:
    _x, _y = _vx + 60, _vy + 60
root.geometry("%dx%d+%d+%d" % (WINDOW_W, WINDOW_H, _x, _y))

f_active = tkfont.Font(root=root, family=font_family, size=font_size, weight="bold")
f_passive = tkfont.Font(root=root, family=font_family, size=max(9, round(font_size * 0.65)))
f_small = tkfont.Font(root=root, family=font_family, size=9)

registry = []


def reg(widget, role):
    registry.append((widget, role))
    return widget


def apply_transparency():
    try:
        root.attributes("-transparentcolor", KEY if transparent else "")
    except tk.TclError:
        pass   # not supported on this platform


def apply_theme():
    th = THEME
    root.configure(bg=canvas_bg())
    canvas.configure(bg=canvas_bg())
    for w, role in registry:
        try:
            if role in ("top", "panel"):
                w.configure(bg=th["top"])
            elif role == "top_label":
                w.configure(bg=th["top"], fg=th["text"])
            elif role == "top_button":
                w.configure(bg=th["top"], fg=th["text"], activebackground=th["button_active"],
                            activeforeground=th["text"])
            elif role == "close":
                w.configure(bg=th["top"], fg=th["text"], activebackground="#c0392b",
                            activeforeground="white")
            elif role == "button":
                w.configure(bg=th["button"], fg=th["text"], activebackground=th["button_active"],
                            activeforeground=th["text"])
            elif role == "entry":
                w.configure(bg=th["entry"], fg=th["text"], insertbackground=th["text"])
            elif role == "panel_info":
                w.configure(bg=th["top"], fg=th["info"])
            elif role == "scale":
                w.configure(bg=th["top"], fg=th["text"], troughcolor=th["button"])
        except tk.TclError:
            pass
    apply_transparency()
    request_redraw()


_drag = {"x": 0, "y": 0, "px": 0, "py": 0}


def drag_start(e):
    _drag["x"] = e.x_root - root.winfo_x()
    _drag["y"] = e.y_root - root.winfo_y()
    _drag["px"], _drag["py"] = e.x_root, e.y_root


def work_area():
    """Primary screen area without the taskbar, as (left, top, right, bottom), or None."""
    try:
        import ctypes.wintypes
        r = ctypes.wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def snap_axis(pos, size, lows, highs):
    """Snap the window start (pos) so its start touches a low edge or its end touches a high edge."""
    best, best_d = pos, SNAP_DISTANCE + 1
    for edge in lows:
        if abs(pos - edge) < best_d:
            best, best_d = edge, abs(pos - edge)
    for edge in highs:
        if abs(pos + size - edge) < best_d:
            best, best_d = edge - size, abs(pos + size - edge)
    return best


def snap_position(x, y):
    if not snap_enabled:
        return x, y
    w, h = root.winfo_width(), root.winfo_height()
    xl, xh = [_vx], [_vx + _vw]
    yl, yh = [_vy], [_vy + _vh]
    wa = work_area()
    if wa:
        xl.append(wa[0]); xh.append(wa[2]); yl.append(wa[1]); yh.append(wa[3])
    return snap_axis(x, w, xl, xh), snap_axis(y, h, yl, yh)


def drag_move(e):
    x, y = snap_position(e.x_root - _drag["x"], e.y_root - _drag["y"])
    root.geometry("+%d+%d" % (x, y))


def was_click(e):
    return abs(e.x_root - _drag["px"]) <= 4 and abs(e.y_root - _drag["py"]) <= 4


def persist():
    try:
        settings["x"] = root.winfo_x()
        settings["y"] = root.winfo_y()
        settings["width"] = root.winfo_width()
        settings["height"] = root.winfo_height()
        settings["opacity"] = opacity_scale.get()
        settings["theme"] = theme_name
        settings["active_color"] = active_color_name
        settings["font"] = font_family
        settings["font_size"] = font_size
        settings["transparent"] = transparent
        settings["autohide"] = autohide
        settings["snap"] = snap_enabled
        save_settings(settings)
    except Exception:
        pass


def close_app():
    persist()
    if tray_icon:
        try:
            tray_icon.stop()
        except Exception:
            pass
    root.destroy()


def hide_window():
    root.withdraw()


def show_window():
    root.deiconify()
    root.attributes("-topmost", True)
    root.lift()


# ---------- Widgets ----------
canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=canvas_bg())

top_bar = reg(tk.Frame(root), "top")
close_btn = reg(tk.Button(top_bar, text="✕", command=close_app, bd=0, font=("Segoe UI", 10), padx=6), "close")
close_btn.pack(side="right", padx=(0, 4))
hide_btn = reg(tk.Button(top_bar, text="−", command=hide_window, bd=0,
                         font=("Segoe UI", 11, "bold"), padx=6), "top_button")
hide_btn.pack(side="right", padx=(0, 2))
settings_btn = reg(tk.Button(top_bar, text="⚙", bd=0, font=("Segoe UI", 10), padx=6), "top_button")
settings_btn.pack(side="right", padx=(0, 2))
title_label = reg(tk.Label(top_bar, text="♪ Lyrics Overlay", font=("Segoe UI", 10, "bold"), anchor="w"),
                  "top_label")
title_label.pack(side="left", fill="x", expand=True, padx=8, pady=5)

panel = reg(tk.Frame(root), "panel")
info_label = reg(tk.Label(panel, text="", font=("Segoe UI", 9), justify="center"), "panel_info")
info_label.pack(fill="x", pady=(6, 2))

search_bar = reg(tk.Frame(panel), "panel")
search_bar.pack(fill="x", padx=10, pady=(0, 6))
search_entry = reg(tk.Entry(search_bar, bd=0, font=("Segoe UI", 9)), "entry")
search_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 4))
search_entry.bind("<Button-1>", lambda e: search_entry.focus_force())


def make_button(txt, cmd, parent):
    return reg(tk.Button(parent, text=txt, command=cmd, bd=0, font=("Segoe UI", 9), padx=6), "button")


def manual_search():
    q = search_entry.get().strip()
    if not q:
        return
    with lock:
        t, a, d = state["title"], state["artist"], state["duration"]
    new_song(t, a, d, manual=q)


def auto_search():
    search_entry.delete(0, "end")
    with lock:
        t, a, d = state["title"], state["artist"], state["duration"]
    if t:
        new_song(t, a, d, force=True)   # skip the saved result and search again


def set_offset(value=None, delta=0.0):
    with lock:
        state["offset"] = value if value is not None else state["offset"] + delta
        t, a, off = state["title"], state["artist"], state["offset"]
    remember_offset(t, a, off)


make_button("Search", manual_search, search_bar).pack(side="left", padx=1)
make_button("Auto", auto_search, search_bar).pack(side="left", padx=1)
make_button("Next ▸", next_result, search_bar).pack(side="left", padx=1)
search_entry.bind("<Return>", lambda e: manual_search())

control_bar = reg(tk.Frame(panel), "panel")
control_bar.pack(fill="x", padx=(10, 24), pady=(0, 8))
make_button("−5s", lambda: set_offset(delta=-5), control_bar).pack(side="left", padx=1)
make_button("−0.5s", lambda: set_offset(delta=-0.5), control_bar).pack(side="left", padx=1)
make_button("Reset", lambda: set_offset(value=0.0), control_bar).pack(side="left", padx=1)
make_button("+0.5s", lambda: set_offset(delta=0.5), control_bar).pack(side="left", padx=1)
make_button("+5s", lambda: set_offset(delta=5), control_bar).pack(side="left", padx=1)

opacity_scale = reg(tk.Scale(
    control_bar, from_=30, to=100, orient="horizontal", showvalue=False, length=90,
    highlightthickness=0, bd=0,
    command=lambda v: root.attributes("-alpha", int(float(v)) / 100),
), "scale")
opacity_scale.set(settings.get("opacity", 93))
opacity_scale.pack(side="right")
reg(tk.Label(control_bar, text="Opacity", font=("Segoe UI", 9)), "panel_info").pack(side="right", padx=(10, 2))

# resize grip (bottom-right corner, visible together with the controls)
grip = tk.Label(root, text="◢", font=("Segoe UI", 9))
for _cur in ("size_nw_se", "bottom_right_corner"):   # name differs between Windows and other systems
    try:
        grip.configure(cursor=_cur)
        break
    except tk.TclError:
        pass
_rs = {"x": 0, "y": 0, "w": 0, "h": 0}


def grip_start(e):
    _rs.update(x=e.x_root, y=e.y_root, w=root.winfo_width(), h=root.winfo_height())


def grip_move(e):
    nw = max(300, _rs["w"] + e.x_root - _rs["x"])
    nh = max(220, _rs["h"] + e.y_root - _rs["y"])
    root.geometry("%dx%d" % (nw, nh))


grip.bind("<ButtonPress-1>", grip_start)
grip.bind("<B1-Motion>", grip_move)


# ---------- Lyrics canvas ----------
_draw = {"sig": None, "rects": [], "gap": 8}


def request_redraw():
    _draw["sig"] = None


def render(items, footer):
    """items: five strings (two before, the current line, two after)."""
    canvas.delete("all")
    W, H = canvas.winfo_width(), canvas.winfo_height()
    if W < 60 or H < 40:
        return
    wrap = max(100, W - 28)
    cx = W / 2.0
    heights = []
    for k, txt in enumerate(items):
        if not txt:
            heights.append(0)
            continue
        tid = canvas.create_text(cx, 0, text=txt, font=f_active if k == 2 else f_passive,
                                 width=wrap, justify="center", anchor="n")
        bb = canvas.bbox(tid)
        heights.append((bb[3] - bb[1]) if bb else 0)
        canvas.delete(tid)
    gap = max(6, int(font_size * 0.5))
    _draw["gap"] = gap
    ha = heights[2] or int(font_size * 1.8)
    ys = [None] * 5
    ys[2] = H / 2.0 - ha / 2.0
    y = ys[2]
    for k in (1, 0):
        if heights[k]:
            y -= gap + heights[k]
            ys[k] = y
    y = ys[2] + ha
    for k in (3, 4):
        if heights[k]:
            y += gap
            ys[k] = y
            y += heights[k]

    rects = []
    oc = outline_color()
    offsets = [(-1, -1), (1, -1), (-1, 1), (1, 1), (0, 2), (-2, 0), (2, 0)] if transparent else []
    for k, txt in enumerate(items):
        if not txt or ys[k] is None:
            continue
        font = f_active if k == 2 else f_passive
        for dx, dy in offsets:
            canvas.create_text(cx + dx, ys[k] + dy, text=txt, font=font, fill=oc, width=wrap,
                               justify="center", anchor="n")
        canvas.create_text(cx, ys[k], text=txt, font=font, fill=line_color(abs(k - 2)), width=wrap,
                           justify="center", anchor="n")
        rects.append((ys[k], ys[k] + heights[k], k))
    _draw["rects"] = rects

    if footer:
        fy = H - 6
        for dx, dy in offsets:
            canvas.create_text(cx + dx, fy + dy, text=footer, font=f_small, fill=oc, anchor="s")
        canvas.create_text(cx, fy, text=footer, font=f_small,
                           fill=THEME["info"] if not transparent else THEME["text"], anchor="s")


def line_at(y):
    best, best_d = None, 10 ** 9
    for y0, y1, k in _draw["rects"]:
        d = 0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
        if d < best_d:
            best, best_d = k, d
    return best if best_d <= _draw["gap"] * 2 else None


def on_line_click(k):
    with lock:
        times = state["lyrics_times"]
        if not times:
            return
        now = song_clock(state)
        idx = bisect_right(times, now + state["offset"]) - 1 + state["scroll_offset"]
        j = idx + k - 2
        if not (0 <= j < len(times)):
            return
        state["offset"] = times[j] + 0.3 - now
        state["scroll_offset"] = 0
        t, a, off = state["title"], state["artist"], state["offset"]
    remember_offset(t, a, off)


def canvas_release(e):
    if not was_click(e):
        return   # it was a drag, not a click
    k = line_at(e.y)
    if k is not None:
        on_line_click(k)


def on_mousewheel(e):
    step = -1 if e.delta > 0 else 1
    with lock:
        times = state["lyrics_times"]
        if not times:
            return
        lt = song_clock(state) + state["offset"]
        target = bisect_right(times, lt) - 1 + state["scroll_offset"] + step
        if target < 0 or target >= len(times):
            return
        state["scroll_offset"] += step
        state["scroll_time"] = time.monotonic()


root.bind("<MouseWheel>", on_mousewheel)
canvas.bind("<ButtonPress-1>", drag_start)
canvas.bind("<B1-Motion>", drag_move)
canvas.bind("<ButtonRelease-1>", canvas_release)
for w in (top_bar, title_label, panel, info_label):
    w.bind("<ButtonPress-1>", drag_start)
    w.bind("<B1-Motion>", drag_move)


# ---------- Settings menu ----------
def change_theme(new_theme):
    global theme_name, THEME
    theme_name = new_theme
    THEME = THEMES[new_theme]
    apply_theme()
    persist()


def change_color(new_color):
    global active_color_name
    active_color_name = new_color
    request_redraw()
    persist()


def change_font(name):
    global font_family
    font_family = name
    for f in (f_active, f_passive, f_small):
        f.configure(family=name)
    request_redraw()
    persist()


def change_size(size):
    global font_size
    font_size = max(12, min(34, size))
    f_active.configure(size=font_size)
    f_passive.configure(size=max(9, round(font_size * 0.65)))
    request_redraw()
    persist()


def toggle_transparent():
    global transparent
    transparent = not transparent
    apply_theme()
    persist()


def toggle_autohide():
    global autohide
    autohide = not autohide
    ctl["visible"] = None   # force relayout
    persist()


def toggle_snap():
    global snap_enabled
    snap_enabled = not snap_enabled
    persist()


def open_log():
    try:
        if not os.path.exists(LOG_FILE):
            log("log started")
        os.startfile(LOG_FILE)
    except Exception:
        messagebox.showinfo("Log file", LOG_FILE)


def open_settings_menu(x=None, y=None):
    global menu_open
    th = THEME
    opts = dict(tearoff=0, bg=th["top"], fg=th["text"], activebackground=th["button_active"],
                activeforeground=th["text"], font=("Segoe UI", 9))
    menu = tk.Menu(root, **opts)

    def mark(on):
        return ("●  " if on else "     ")

    m_theme = tk.Menu(menu, **opts)
    for t in THEMES:
        m_theme.add_command(label=mark(t == theme_name) + THEMES[t].get("label", t.capitalize()),
                            command=lambda t=t: change_theme(t))
    menu.add_cascade(label="Theme", menu=m_theme)

    m_col = tk.Menu(menu, **opts)
    for c_name, c_info in COLORS.items():
        m_col.add_command(label=mark(c_name == active_color_name) + c_info[0],
                          command=lambda c=c_name: change_color(c))
    menu.add_cascade(label="Active line color", menu=m_col)

    m_font = tk.Menu(menu, **opts)
    for fname in FONTS:
        m_font.add_command(label=mark(fname == font_family) + fname, command=lambda f=fname: change_font(f))
    menu.add_cascade(label="Font", menu=m_font)

    m_size = tk.Menu(menu, **opts)
    for label, sz in SIZES:
        m_size.add_command(label=mark(font_size == sz) + label, command=lambda sz=sz: change_size(sz))
    menu.add_cascade(label="Text size", menu=m_size)

    menu.add_separator()
    menu.add_command(label=mark(transparent) + "Transparent background", command=toggle_transparent)
    menu.add_command(label=mark(autohide) + "Auto-hide controls", command=toggle_autohide)
    menu.add_command(label=mark(snap_enabled) + "Snap to screen edges", command=toggle_snap)
    menu.add_separator()
    menu.add_command(label="Next lyrics result", command=next_result)
    menu.add_command(label="Clear saved lyrics", command=cache_clear)
    menu.add_command(label="Open log file", command=open_log)

    if x is None:
        x = settings_btn.winfo_rootx()
        y = settings_btn.winfo_rooty() + settings_btn.winfo_height()
    menu_open = True
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()
        menu_open = False


settings_btn.configure(command=open_settings_menu)
canvas.bind("<Button-3>", lambda e: open_settings_menu(e.x_root, e.y_root))
apply_theme()


# ---------- Show / hide the controls ----------
ctl = {"visible": None, "last_inside": 0.0}


def layout():
    show = ctl["visible"]
    top_h = top_bar.winfo_reqheight()
    panel_h = panel.winfo_reqheight()
    if show:
        top_bar.place(x=0, y=0, relwidth=1)
        panel.place(x=0, rely=1.0, anchor="sw", relwidth=1)
        grip.configure(bg=THEME["top"], fg=THEME["info"])
        grip.place(relx=1.0, rely=1.0, anchor="se")
        if autohide:
            canvas.place(x=0, y=0, relwidth=1, relheight=1)      # controls float over the lyrics
        else:
            canvas.place(x=0, y=top_h, relwidth=1, relheight=1, height=-(top_h + panel_h))
        top_bar.lift()
        panel.lift()
        grip.lift()
    else:
        for w in (top_bar, panel, grip):
            w.place_forget()
        canvas.place(x=0, y=0, relwidth=1, relheight=1)
    request_redraw()


def pointer_inside():
    try:
        px, py = root.winfo_pointerxy()
        x0, y0 = root.winfo_rootx(), root.winfo_rooty()
        return x0 <= px < x0 + root.winfo_width() and y0 <= py < y0 + root.winfo_height()
    except tk.TclError:
        return False


def entry_has_focus():
    try:
        return root.focus_get() is search_entry
    except Exception:
        return False


def update_controls():
    now = time.monotonic()
    if pointer_inside() or menu_open or entry_has_focus():
        ctl["last_inside"] = now
    want = (not autohide) or (now - ctl["last_inside"] < HIDE_DELAY)
    if want != ctl["visible"]:
        ctl["visible"] = want
        layout()


# ---------- System tray ----------
def create_tray():
    """Tray menu actions only queue work; the Tk thread executes it."""
    global tray_icon
    if pystray is None:
        return False
    try:
        img = Image.new("RGB", (64, 64), THEME["bg"])
        d = ImageDraw.Draw(img)
        col = active_color()
        d.ellipse((16, 40, 32, 56), fill=col)
        d.line((32, 48, 32, 16), fill=col, width=4)
        d.line((32, 16, 48, 20), fill=col, width=4)
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda icon, item: ui_queue.put(show_window), default=True),
            pystray.MenuItem("Hide", lambda icon, item: ui_queue.put(hide_window)),
            pystray.MenuItem("Exit", lambda icon, item: ui_queue.put(close_app)),
        )
        tray_icon = pystray.Icon("LyricsOverlay", img, "Lyrics Overlay", menu)
        threading.Thread(target=tray_icon.run, daemon=True).start()
        return True
    except Exception as e:
        log("tray failed: " + repr(e))
        tray_icon = None
        return False


if not create_tray():
    hide_btn.pack_forget()   # without a tray icon, hiding would lose the window


# ---------- Refresh loop ----------
def set_text(lb, txt):
    if lb.cget("text") != txt:
        lb.config(text=txt)


def refresh():
    while True:
        try:
            fn = ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            fn()
        except Exception as e:
            log("ui action failed: " + repr(e))

    update_controls()

    with lock:
        s = dict(state)
    times, lines, scroll = s["lyrics_times"], s["lyrics_lines"], s["scroll_offset"]

    if scroll and time.monotonic() - s["scroll_time"] > SCROLL_RETURN_SECONDS:
        with lock:
            state["scroll_offset"] = 0
        scroll = 0

    now = song_clock(s)
    lt = now + s["offset"]

    if ctl["visible"]:
        if s["has_media"] and s["title"]:
            name = s["title"] if len(s["title"]) < 48 else s["title"][:45] + "..."
            set_text(title_label, "♪ " + name)
        else:
            set_text(title_label, "♪ Lyrics Overlay")
        if s["has_media"]:
            info = "Song: " + fmt_time(now) + " / " + fmt_time(s["duration"])
            info += "\nLyrics: " + fmt_time(lt)
            if s["rec_duration"]:
                info += " / " + fmt_time(s["rec_duration"])
            info += " (offset " + format(s["offset"], "+.1f") + " s)"
            if times:
                k = bisect_right(times, lt) - 1
                if k >= 0:
                    info += "  line start: " + fmt_time(times[k])
            if s["source"]:
                info += "\nSource: " + s["source"]
            set_text(info_label, info)
        else:
            set_text(info_label, "")

    if times:
        base = bisect_right(times, lt) - 1
        idx = max(-1, min(base + scroll, len(times) - 1))
        items = []
        for k in range(5):
            j = idx + k - 2
            if k == 2 and idx < 0:
                items.append("♪")
            elif 0 <= j < len(lines):
                items.append(lines[j] or "♪")
            else:
                items.append("")
    else:
        items = ["", "", s["message"], "", ""]

    footer = ""
    if scroll:
        footer = "browsing - click a line to sync"
    elif times or s["note"]:
        n_alts = len(s["alts"])
        head = ("result %d/%d" % (s["alt_idx"] + 1, n_alts)) if n_alts > 1 else ""
        footer = " - ".join(x for x in (head, s["note"]) if x)
        footer = footer if len(footer) < 80 else footer[:77] + "..."

    sig = (tuple(items), footer, canvas.winfo_width(), canvas.winfo_height(), theme_name,
           active_color_name, font_family, font_size, transparent)
    if sig != _draw["sig"]:
        render(items, footer)
        _draw["sig"] = sig
    root.after(100, refresh)


refresh()
root.mainloop()
