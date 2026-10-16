# 🎵 Lyrics Overlay

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](https://www.microsoft.com/windows)

A lightweight Windows application that displays synced lyrics of the currently playing song (YouTube, Spotify, or any media player that supports Windows Media Controls) in a small always-on-top window.

![Lyrics Overlay Screenshot](screenshot.png)

## ✨ Features

- 🎵 **Automatic detection** of the currently playing song via Windows Media API
- 🔍 **Two lyrics sources**: lrclib.net and MusicXMatch (as fallback)
- ⏱️ **Synced lyrics** displayed in real time
- 🖱️ **Manual search** if the song isn't found automatically
- ⚙️ **Adjustable offset** (±0.5s, ±5s, reset) for fine-tuning sync
- 📜 **Mouse-wheel scrolling** to browse lyrics
- 👆 **Click any line** to set it as the current sync point
- 🎨 **Three themes**: Dark, Light, Blue
- 🌈 **Seven active-line colors**: Yellow, Orange, Green, Blue, Pink, White, Red
- 🔆 **Opacity control** (30% – 100%)
- 💾 **Settings are remembered** (position, size, opacity, theme, color)
- 📌 **System tray icon** with Show / Hide / Exit
- 🪟 **Draggable window** — move it anywhere
- 🚫 **No ads, no tracking, no accounts**

## 📋 Requirements

- Windows 10 or Windows 11
- Python 3.8 or newer
- Packages listed in requirements.txt

## 🚀 Installation

1. Clone or download this repository.
2. Install the required packages: `pip install -r requirements.txt`
3. Run the application: `python lyrics_overlay.pyw` or just double-click the file.

## 🎮 Usage

- Start playing a song on YouTube, Spotify, or any supported media player.
- The lyrics will appear automatically.
- If the wrong lyrics appear or the sync is off:
  - Use the **Search** box to manually enter the song name.
  - Use the **±0.5s** / **±5s** buttons to fine-tune the offset.
  - **Click any lyric line** to set it as the current sync point.
  - Use the **mouse wheel** to scroll through the lyrics.
- Click the **⚙** icon to change the theme or the active-line color.
- Click the **−** icon to hide the window to the system tray.
- Click the **✕** icon to exit.

## ⚙️ Settings

Settings are stored in:

%USERPROFILE%\.lyrics_overlay_settings.json

Delete this file to reset the application.

## 📄 License

This project is licensed under the MIT License — see the LICENSE file for details.

## 🙏 Credits

- Lyrics from lrclib.net and MusicXMatch
- Built with winsdk, musicxmatch-api, pystray, and Pillow

## ⭐ Support

If you find this project useful, please consider giving it a star on GitHub!