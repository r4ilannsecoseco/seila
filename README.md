# seila

Transfer music to iPod Shuffle 3G on Linux with high quality.

## Features

- **Smart sync** — only transfers new/changed files (uses mtime + size cache)
- **Auto-conversion** — FLAC/OGG/WAV/WMA → ALAC (lossless), keeps MP3/M4A as-is
- **Playlist generation** — creates per-artist playlists for VoiceOver navigation
- **Apple Music playlists** — extract tracks from any public Apple Music URL
- **Bulk download** — download from a text file (Artist - Title per line) via yt-dlp
- **iTunesSD rebuild** — regenerates the iPod database so tracks appear on device
- **Dry-run mode** — preview changes before transferring

## Requirements

- Python 3.10+
- ffmpeg (for audio conversion)
- yt-dlp (for downloading from YouTube)
- mutagen (`pip install mutagen`)
- requests (`pip install requests`)

### Install dependencies

```bash
pip install -r requirements.txt
sudo pacman -S ffmpeg yt-dlp python-mutagen   # Arch
sudo apt install ffmpeg yt-dlp python3-mutagen # Debian/Ubuntu
```

## Usage

```
usage: seila.py [-h] [--sync] [--dry-run] [--yes] [--device DEVICE]
                [--playlist PLAYLIST] [--from-file FROM_FILE] [--limit LIMIT]
                [{transfer,list,info,download,dl,clean}] [source]

Commands:
  transfer       Sync music to iPod (default)
  download, dl   Download music from YouTube or Apple Music
  clean          Remove all music from iPod
  info           Show iPod info only
  list           List iPod contents

Options:
  --sync         Only transfer new/changed files
  --dry-run      Preview without transferring
  --yes, -y      Skip confirmation prompts
  --device       iPod mount path (e.g. /run/media/user/IPOD)
  --playlist     Apple Music playlist URL or ID
  --from-file    Text file with Artist - Title per line
  --limit N      Limit downloads to N tracks
```

### Examples

```bash
# Full workflow: download then sync
seila download --from-file playlist.txt --yes
seila transfer --sync

# From Apple Music
seila download --playlist "https://music.apple.com/playlist/pl.xxxxx"
seila transfer

# Clear iPod and start fresh
seila clean

# Single track
seila download "Artist - Song Name"
```

## How it works

1. Scans your music folder for audio files
2. Compares against a cache to find new/changed files
3. Converts lossy formats to ALAC (iPod Shuffle 3G native)
4. Copies files to iPod using 4-letter naming scheme
5. Rebuilds the iTunesSD database (558-byte entries per track)
6. Generates artist-based playlists for VoiceOver navigation

## iPod Shuffle 3G specifics

- FAT32 filesystem
- Supports ALAC, MP3, AAC, WAV
- ~2 GB capacity
- No display — uses VoiceOver for navigation
- Database file: `iPod_Control/Device/SysInfoExtended2`
