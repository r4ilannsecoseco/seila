#!/usr/bin/env python3
"""
seila - Transfer music to iPod Shuffle 3G on Linux

Usage:
    seila download --from-file playlist.txt --yes
    seila transfer --sync
    seila clean
"""
import argparse
import os
import sys
import subprocess
import json
import hashlib
import re
import shutil
from pathlib import Path
from datetime import datetime

# Constants
SOURCE_DIR = Path.home() / "Music"
CACHE_DIR_NAME = ".seila_cache"
CACHE_FILE = "cache.json"
MUSIC_DIR_ON_IPOD = "iPod_Control/Music"
ITUNES_SD_PATH = "iPod_Control/iTunes/iTunesSD"
ITUNES_PSTATE_PATH = "iPod_Control/iTunes/iTunesPState"
ITUNES_STATS_PATH = "iPod_Control/iTunes/iTunesStats"
ITUNES_SHUFFLE_PATH = "iPod_Control/iTunes/iTunesShuffle"

# Supported audio extensions
AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac", ".wma"}
# Extensions that need conversion (not natively supported by Shuffle 3G)
NEEDS_CONVERSION = {".flac", ".ogg", ".wav", ".wma"}
# Extensions that can be copied as-is
CAN_COPY = {".mp3", ".m4a", ".aac"}


def get_user():
    """Get current username reliably."""
    return os.environ.get("USER", "")


def detect_ipod():
    """Detect if an iPod Shuffle is mounted and return its mount point."""
    user = get_user()

    # Scan common mount point directories dynamically
    for base in [Path(f"/run/media/{user}"), Path(f"/media/{user}")]:
        if not base.exists():
            continue
        for mount in base.iterdir():
            if (mount / "iPod_Control").exists():
                return mount

    # Try static mount points
    for p in [Path("/mnt/ipod"), Path("/mnt/IPOD")]:
        if p.exists() and (p / "iPod_Control").exists():
            return p

    # Try to detect via gio mount list
    try:
        result = subprocess.run(["gio", "mount", "-l"],
                                capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.split()
            for part in parts:
                path = Path(part.strip())
                if path.exists() and (path / "iPod_Control").exists():
                    return path
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def get_ipod_info(mount_point):
    """Get basic info about the iPod: capacity, free space, etc."""
    try:
        usage = subprocess.check_output(["df", "-k", str(mount_point)],
                                        text=True).splitlines()[1]
        parts = usage.split()
        total_kb = int(parts[1])
        used_kb = int(parts[2])
        free_kb = int(parts[3])
        percent_used = parts[4].rstrip("%")
        return {
            "total": total_kb * 1024,
            "used": used_kb * 1024,
            "free": free_kb * 1024,
            "percent_used": percent_used,
        }
    except (subprocess.CalledProcessError, IndexError, ValueError):
        return None


def scan_music_folder(source_dir):
    """Scan source directory for music files and return metadata."""
    music_files = []
    seen = set()

    for f in source_dir.rglob("*"):
        if f.suffix.lower() in AUDIO_EXTENSIONS and f.is_file() and f not in seen:
            seen.add(f)
            try:
                stat = f.stat()
                music_files.append({
                    "path": f,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "ext": f.suffix.lower(),
                })
            except (OSError, PermissionError):
                continue

    return sorted(music_files, key=lambda x: x["path"])


def needs_conversion(ext):
    """Check if file extension needs conversion."""
    return ext in NEEDS_CONVERSION


def convert_to_alac(src_path, dst_path):
    """Convert audio file to ALAC (Apple Lossless) using ffmpeg."""
    dst_path = dst_path.with_suffix(".m4a")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_path),
        "-c:a", "alac",
        "-movflags", "+faststart",
        str(dst_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return True, dst_path
        return False, None
    except (subprocess.TimeoutExpired, Exception):
        return False, None


def copy_file(src_path, dst_path):
    """Copy file preserving metadata."""
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        return True
    except Exception:
        return False


def get_file_quick_hash(filepath):
    """Quick hash based on size + mtime + first/last 4KB. Much faster than full SHA256."""
    try:
        stat = filepath.stat()
        return f"{stat.st_size}-{int(stat.st_mtime)}"
    except OSError:
        return None


def load_cache(source_dir):
    """Load cache file from source directory."""
    cache_dir = source_dir / CACHE_DIR_NAME
    cache_path = cache_dir / CACHE_FILE
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "files": {}}
    return {"version": 1, "files": {}}


def save_cache(source_dir, cache):
    """Save cache file to source directory."""
    cache_dir = source_dir / CACHE_DIR_NAME
    cache_dir.mkdir(exist_ok=True)
    cache_path = cache_dir / CACHE_FILE
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)


def make_file_key(source_path, source_dir):
    """Create a unique key for a music file based on its relative path."""
    rel = source_path.relative_to(source_dir)
    return str(rel)


def compare_files(source_files, cache, source_dir):
    """Compare source files with cache. Return lists of new, changed, unchanged."""
    new_files = []
    changed_files = []
    unchanged_files = []

    cached_files = cache.get("files", {})

    for sf in source_files:
        key = make_file_key(sf["path"], source_dir)
        cached = cached_files.get(key)

        if cached is None:
            new_files.append(sf)
        elif cached.get("mtime") != sf["mtime"] or cached.get("size") != sf["size"]:
            changed_files.append(sf)
        else:
            unchanged_files.append(sf)

    return new_files, changed_files, unchanged_files


def format_size(size_bytes):
    """Format bytes to human readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def print_summary(new_files, changed_files, unchanged_files, ipod_info):
    """Print transfer summary and ask for confirmation."""
    total_new = sum(f["size"] for f in new_files)
    total_changed = sum(f["size"] for f in changed_files)
    total_transfer = total_new + total_changed

    print(f"\n{'='*50}")
    print(f"{'Ação':<25} {'Arquivos':>8} {'Tamanho':>10}")
    print(f"{'='*50}")
    print(f"{'[NOVOS]':<25} {len(new_files):>8} {format_size(total_new):>10}")
    print(f"{'[ATUALIZ]':<25} {len(changed_files):>8} {format_size(total_changed):>10}")
    print(f"{'[SINCRON]':<25} {len(unchanged_files):>8} {format_size(sum(f['size'] for f in unchanged_files)):>10}")
    print(f"{'='*50}")

    total_all = total_new + total_changed + sum(f["size"] for f in unchanged_files)
    print(f"{'Total no fonte:':<25} {'':>8} {format_size(total_all):>10}")
    print(f"{'A transferir:':<25} {'':>8} {format_size(total_transfer):>10}")

    if ipod_info:
        free = ipod_info["free"]
        print(f"{'Espaço livre no iPod:':<25} {'':>8} {format_size(free):>10}")
        if total_transfer > free:
            print(f"\n[!] AVISO: Não cabe no iPod! Faltam {format_size(total_transfer - free)}")
            return False
        else:
            print(f"{'Espaço após transfer:':<25} {'':>8} {format_size(free - total_transfer):>10}")

    return True


def rebuild_itunes_sd(ipod_mount, tracks):
    """Rebuild iTunesSD database file for iPod Shuffle."""
    sd_path = ipod_mount / ITUNES_SD_PATH
    sd_path.parent.mkdir(parents=True, exist_ok=True)

    with open(sd_path, "wb") as f:
        # Header: 18 bytes
        header = bytearray(18)
        header[3] = len(tracks) >> 8
        header[4] = len(tracks) & 0xFF
        header[6] = 1  # version
        header[8] = 18
        f.write(header)

        # Each entry: 558 bytes
        for track in tracks:
            entry = bytearray(558)
            # File type byte
            ext = track["ext"]
            if ext == ".mp3":
                entry[29] = 1
            elif ext in (".m4a", ".aac"):
                entry[29] = 2
            elif ext == ".wav":
                entry[29] = 4
            else:
                entry[29] = 1

            # Filename (UTF-16LE, max 261 chars)
            filename = track["ipod_filename"].encode("utf-16-le")[:260]
            for i, byte_val in enumerate(filename):
                entry[33 + i] = byte_val

            # Shuffle flag
            entry[555] = 1

            f.write(entry)


def rebuild_pstate(ipod_mount):
    """Rebuild iTunesPState (playback state)."""
    pstate_path = ipod_mount / ITUNES_PSTATE_PATH
    pstate_path.parent.mkdir(parents=True, exist_ok=True)

    pstate = bytearray(21)
    pstate[0] = 29  # volume
    pstate[6] = 1
    pstate[18] = 1
    with open(pstate_path, "wb") as f:
        f.write(pstate)


def rebuild_stats(ipod_mount, track_count):
    """Rebuild iTunesStats (play statistics)."""
    stats_path = ipod_mount / ITUNES_STATS_PATH
    stats_path.parent.mkdir(parents=True, exist_ok=True)

    track_count_bytes = track_count.to_bytes(3, byteorder="little")
    stats_data = track_count_bytes + b"\x00" * 3 + (b"\x12\x00\x00\x00\x00" + b"\xff" * 3 + b"\x00" * 12) * track_count
    with open(stats_path, "wb") as f:
        f.write(stats_data)


def rebuild_shuffle(ipod_mount, track_count):
    """Rebuild iTunesShuffle (shuffle sequence)."""
    shuffle_path = ipod_mount / ITUNES_SHUFFLE_PATH
    shuffle_path.parent.mkdir(parents=True, exist_ok=True)

    import random
    random.seed()
    seq = list(range(track_count))
    random.shuffle(seq)

    with open(shuffle_path, "wb") as f:
        for idx in seq:
            f.write(idx.to_bytes(3, byteorder="little"))


def generate_playlists(ipod_mount, tracks):
    """Generate playlists on the iPod based on artist folder structure."""
    playlists_dir = ipod_mount / "iPod_Control" / "Music" / "Playlists"
    playlists_dir.mkdir(parents=True, exist_ok=True)

    # Clear old playlists
    for f in playlists_dir.glob("*.m3u"):
        f.unlink()

    # Group tracks by artist
    artists = {}
    for track in tracks:
        artist = track.get("artist", "Desconhecido")
        if artist not in artists:
            artists[artist] = []
        artists[artist].append(track)

    # Create playlist files
    playlist_idx = 1
    for artist_name, artist_tracks in sorted(artists.items()):
        playlist_file = playlists_dir / f"{playlist_idx:04d}.m3u"
        with open(playlist_file, "w") as f:
            f.write(f"#PLAYLIST:{artist_name}\n")
            for t in artist_tracks:
                f.write(f"{t['ipod_filename']}\n")
        playlist_idx += 1


# ─── Apple Music Playlist Extractor ───────────────────────────────────────────

def extract_apple_music_playlist(url_or_id):
    """Extract track list from an Apple Music playlist URL or ID.

    Uses JSON-LD embedded in the page + per-song meta description (no API key).
    Returns dict with name, description, tracks (list of {title, artists}).
    """
    import requests

    # Parse URL to get full playlist URL
    playlist_url = url_or_id
    if not url_or_id.startswith("http"):
        playlist_url = f"https://music.apple.com/us/playlist/x/{url_or_id}"

    try:
        page = requests.get(
            playlist_url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        page.raise_for_status()
    except requests.RequestException as e:
        print(f"[X] Erro ao acessar playlist: {e}")
        return None

    # Extract JSON-LD from playlist page (Apple Music uses plain script tags, not type=ld+json)
    html = page.text
    m = re.search(
        r'<script[^>]*>(.*?MusicPlaylist.*?)</script>',
        html, re.DOTALL
    )
    if not m:
        # Fallback: try matching any JSON block with MusicPlaylist
        m = re.search(r'\{[^}]*"@type"\s*:\s*"MusicPlaylist"[^}]*\}', html)
        if not m:
            print("[X] JSON-LD não encontrado na página")
            return None

    data = json.loads(m.group(1))
    if isinstance(data, dict) and data.get("@type") == "MusicPlaylist":
        pass
    elif isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and d.get("@type") == "MusicPlaylist"), None)
    if not data:
        print("[X] Dados da playlist não encontrados")
        return None

    name = data.get("name", "Unknown Playlist")
    description = data.get("description", "")
    raw_tracks = data.get("track", [])
    tracks = []

    # Helper: extract artist via meta description on song page
    def fetch_artist(song_url, song_title, session):
        try:
            resp = session.get(song_url, timeout=10)
            m = re.search(
                r'<meta\s+name="description"\s+content="Listen\s+to\s+' +
                re.escape(song_title) + r'\s+by\s+(.+?)\s+on\s+Apple\s+Music',
                resp.text
            )
            if m:
                return m.group(1)
        except requests.RequestException:
            pass
        return None

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    playlist_id = re.search(r"pl\.[a-f0-9]+", playlist_url).group(0) if re.search(r"pl\.[a-f0-9]+", playlist_url) else ""

    for i, track in enumerate(raw_tracks, 1):
        title = track.get("name", "")
        song_url = track.get("url", "")
        print(f"  [{i}/{len(raw_tracks)}] Obtendo artista para: {title}", end="\r")
        artist = fetch_artist(song_url, title, session)
        if artist:
            tracks.append({"title": title, "artists": artist})
        else:
            tracks.append({"title": title, "artists": title})

    print(" " * 70, end="\r")
    return {
        "name": name,
        "description": description,
        "playlistID": playlist_id,
        "tracks": tracks,
    }


def download_with_ytdlp(artist, title, output_dir, quality="best"):
    """Download a song from YouTube Music using yt-dlp.

    Falls back to title-only search if artist is empty or same as title.
    Returns the path to the downloaded file, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    query = f"{artist} - {title}" if artist and artist != title else title

    # Create safe filename template
    safe = re.sub(r'[^\w\- ]', '', query).strip()[:80]
    template = str(output_dir / f"{safe}.%(ext)s")

    cmd = [
        "yt-dlp", "--quiet", "--no-warnings",
        "-f", "bestaudio[ext=m4a]/bestaudio",
        "--extract-audio", "--audio-format", "m4a",
        "--audio-quality", "0",
        "--embed-metadata",
        "--add-metadata",
        "-o", template,
        f"ytsearch:{query}",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return None

        # Find the downloaded file
        for f in output_dir.iterdir():
            if f.stem.startswith(safe) and f.suffix in (".m4a", ".mp3"):
                return f
        return None
    except (subprocess.TimeoutExpired, Exception):
        return None


def cmd_download_playlist(playlist_arg, source_dir, yes, limit=0):
    """Download tracks from an Apple Music playlist."""
    print(f"[♪] Extraindo playlist Apple Music...")
    playlist = extract_apple_music_playlist(playlist_arg)
    if not playlist:
        return

    tracks = playlist["tracks"][:limit] if limit > 0 else playlist["tracks"]
    print(f"   Playlist: {playlist['name']}")
    print(f"   Músicas:  {len(tracks)}")

    if not yes:
        confirm = input(f"\nBaixar {len(tracks)} músicas? [S/n]: ").strip().lower()
        if confirm and confirm != "s":
            print("Cancelado.")
            return

    playlist_dir = source_dir / playlist["name"]
    playlist_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    failed = 0
    total = len(tracks)
    for i, track in enumerate(tracks, 1):
        artist = track["artists"]
        title = track["title"]
        print(f"  [{i}/{total}] {artist} - {title}", end="")

        result = download_with_ytdlp(artist, title, playlist_dir)
        if result:
            print(f" -> {result.name}")
            downloaded += 1
        else:
            print(" -> [X]")
            failed += 1

            print(f"\n[OK] Download concluido: {downloaded} baixadas, {failed} falhas")
    print(f"[DIR] Pasta: {playlist_dir}")
    print(f"[i] Depois rode: seila transfer --sync")


def cmd_download_single(track_arg, source_dir):
    """Download a single track by artist + name."""
    if " - " not in track_arg:
        print("[X] Use o formato: Artista - Nome da Música")
        return
    artist, title = track_arg.split(" - ", 1)
    result = download_with_ytdlp(artist.strip(), title.strip(), source_dir)
    if result:
        print(f"[OK] Download: {result.name}")
    else:
        print("[X] Falha no download")


def cmd_download_from_file(file_path, source_dir, yes, limit=0):
    """Download tracks from a .txt file with Artist - Title per line."""
    file_path = Path(file_path)
    if not file_path.exists():
        print(f"[X] Arquivo '{file_path}' não encontrado")
        return

    lines = file_path.read_text(encoding="utf-8").strip().splitlines()
    lines = [l.strip() for l in lines if l.strip()]
    if limit > 0:
        lines = lines[:limit]

    print(f"[*] Lendo {len(lines)} músicas de: {file_path.name}")

    if not yes:
        confirm = input(f"\nBaixar {len(lines)} músicas? [S/n]: ").strip().lower()
        if confirm and confirm != "s":
            print("Cancelado.")
            return

    downloaded = 0
    failed = 0
    total = len(lines)
    for i, line in enumerate(lines, 1):
        if " - " not in line:
            print(f"  [{i}/{total}] {line} -> [PULA] (formato inválido)")
            failed += 1
            continue

        artist, title = line.split(" - ", 1)
        artist = artist.strip()
        title = title.strip()
        print(f"  [{i}/{total}] {artist} - {title}", end="")

        result = download_with_ytdlp(artist, title, source_dir)
        if result:
            print(f" -> {result.name}")
            downloaded += 1
        else:
            print(" -> [X]")
            failed += 1

    print(f"\n[OK] Download concluido: {downloaded} baixadas, {failed} falhas")
    print(f"[DIR] Pasta: {source_dir}")


def cmd_clean_ipod(ipod_mount, source_dir):
    """Remove all music files from iPod and reset database."""
    print(f"[#] Limpando iPod em {ipod_mount}...")

    music_dir = ipod_mount / MUSIC_DIR_ON_IPOD
    if not music_dir.exists():
        print("Nenhuma música encontrada no iPod.")
        return

    files = list(music_dir.rglob("*"))
    audio_files = [f for f in files if f.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac")]
    if not audio_files:
        print("Nenhum arquivo de áudio encontrado.")
        return

    print(f"  {len(audio_files)} arquivos de áudio encontrados")
    confirm = input("Tem certeza? Todas as músicas serão apagadas. [s/N]: ").strip().lower()
    if confirm != "s":
        print("Cancelado.")
        return

    # Remove audio files
    for f in audio_files:
        f.unlink()
    print(f"  [OK] {len(audio_files)} arquivos removidos")

    # Remove empty subdirectories
    for f in sorted(files, key=lambda x: len(str(x)), reverse=True):
        if f.is_dir() and not any(f.iterdir()):
            f.rmdir()

    # Reset database
    sd_path = ipod_mount / ITUNES_SD_PATH
    if sd_path.exists():
        sd_path.unlink()
    rebuild_itunes_sd(ipod_mount, [])
    print("  [OK] Database iTunesSD resetada")

    # Clear cache
    cache_dir = source_dir / ".seila_cache"
    if cache_dir.exists():
        import shutil
        shutil.rmtree(cache_dir)
        print("  [OK] Cache limpo")

    print("[OK] iPod limpo com sucesso! Pronto para nova transferência.")


def main():
    parser = argparse.ArgumentParser(description="Transfer music to iPod Shuffle 3G")
    parser.add_argument("command", nargs="?", default="transfer",
                        choices=["transfer", "list", "info", "download", "dl", "clean"],
                        help="Command to run (default: transfer)")
    parser.add_argument("source", nargs="?", default=str(SOURCE_DIR),
                        help=f"Source music directory (default: {SOURCE_DIR})")
    parser.add_argument("--sync", action="store_true",
                        help="Sync mode: only transfer new/changed files")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without doing it")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Assume yes to all prompts")
    parser.add_argument("--device",
                        help="Caminho do iPod montado (ex: /run/media/user/IPOD)")
    parser.add_argument("--playlist",
                        help="URL ou ID de playlist Apple Music para baixar")
    parser.add_argument("--from-file",
                        help="Arquivo .txt com lista de músicas (Artista - Título por linha)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limitar número de downloads (0 = sem limite)")
    args = parser.parse_args()

    source_dir = Path(args.source).resolve()

    # ── download command ─────────────────────────────────────────────────────
    if args.command in ("download", "dl"):
        if args.playlist:
            cmd_download_playlist(args.playlist, source_dir, args.yes, args.limit)
        elif args.from_file:
            cmd_download_from_file(args.from_file, source_dir, args.yes, args.limit)
        elif " - " in str(args.source).strip():
            cmd_download_single(str(args.source).strip(), source_dir)
        else:
            print("[X] Use --playlist <URL>, --from-file <arquivo>, ou passe \"Artista - Música\"")
        return

    # ── clean command ────────────────────────────────────────────────────────
    if args.command == "clean":
        if args.device:
            ipod_mount = Path(args.device)
        else:
            print("[?] Detectando iPod...")
            ipod_mount = detect_ipod()
        if not ipod_mount or not ipod_mount.exists():
            print("[X] iPod não encontrado. Use --device pra especificar o caminho.")
            sys.exit(1)
        cmd_clean_ipod(ipod_mount, source_dir)
        return

    # ── remaining commands need iPod ─────────────────────────────────────────
    if not source_dir.exists():
        print(f"[X] Pasta '{source_dir}' não existe")
        sys.exit(1)

    if args.device:
        ipod_mount = Path(args.device)
        if not ipod_mount.exists():
            print(f"[X] Caminho '{ipod_mount}' não existe")
            sys.exit(1)
        print(f"[DIR] Usando device: {ipod_mount}")

    else:
        print("[?] Detectando iPod...")
        ipod_mount = detect_ipod()
        if not ipod_mount:
            print("[X] Nenhum iPod Shuffle detectado.")
            print("   Execute 'lsblk -f' pra encontrar o caminho")
            print("   e use: --device /run/media/seuuser/IPOD")
            sys.exit(1)

    print(f"[OK] iPod encontrado em: {ipod_mount}")

    # Get iPod info
    ipod_info = get_ipod_info(ipod_mount)
    if ipod_info:
        free_gb = ipod_info["free"] / (1024**3)
        total_gb = ipod_info["total"] / (1024**3)
        used_gb = ipod_info["used"] / (1024**3)
        print(f"[DISK] iPod: {used_gb:.1f} GB usado de {total_gb:.1f} GB "
              f"({free_gb:.1f} GB livre)")

    if args.command == "info":
        return

    # Scan music folder
    print(f"[?] Escaneando {source_dir}...")
    music_files = scan_music_folder(source_dir)
    print(f"[♪] {len(music_files)} arquivos de áudio encontrados")

    if not music_files:
        print("Nenhum arquivo de áudio encontrado na pasta de origem.")
        return

    # Load cache and compare
    cache = load_cache(source_dir)
    new_files, changed_files, unchanged_files = compare_files(music_files, cache, source_dir)

    # Summary
    fits = print_summary(new_files, changed_files, unchanged_files, ipod_info)

    if not fits:
        print("\n[X] Transferência cancelada por falta de espaço.")
        return

    # Confirm
    if not args.yes:
        confirm = input("\nTransferir? [S/n]: ").strip().lower()
        if confirm and confirm != "s":
            print("Cancelado.")
            return

    if args.dry_run:
        print("\n[Dry-run] Nenhuma alteração feita.")
        return

    # Prepare tracks for iPod
    all_files = new_files + changed_files
    if not all_files:
        print("\n[OK] Tudo já sincronizado! Nada a transferir.")
        return

    # Transfer files
    print(f"\n[>>] Transferindo {len(all_files)} arquivos...")
    cache_new = dict(cache.get("files", {}))
    transferred = 0

    for i, mf in enumerate(all_files):
        src = mf["path"]
        key = make_file_key(src, source_dir)

        # Generate iPod filename (4 letter code)
        ipod_folder = f"F{i % 16:01X}{(i // 16) % 16:01X}"
        ipod_name = f"F{chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}{chr(65 + (i // 676) % 26)}{chr(65 + (i // 17576) % 26)}"
        dst = ipod_mount / MUSIC_DIR_ON_IPOD / ipod_folder / f"{ipod_name}{mf['ext']}"

        progress = f"[{i+1}/{len(all_files)}]"
        print(f"  {progress} {src.name}", end=" ")

        if needs_conversion(mf["ext"]):
            ok, converted_path = convert_to_alac(src, dst)
            if ok:
                print("-> ALAC OK")
                cache_new[key] = {
                    "source_path": str(src.relative_to(source_dir)),
                    "ipod_filename": f"{ipod_folder}/{ipod_name}.m4a",
                    "size": converted_path.stat().st_size,
                    "mtime": mf["mtime"],
                    "ext": mf["ext"],
                    "converted": True,
                }
                transferred += 1
            else:
                print("-> ERRO na conversao [X]")
        else:
            if copy_file(src, dst):
                print("-> copiado OK")
                cache_new[key] = {
                    "source_path": str(src.relative_to(source_dir)),
                    "ipod_filename": f"{ipod_folder}/{ipod_name}{mf['ext']}",
                    "size": mf["size"],
                    "mtime": mf["mtime"],
                    "ext": mf["ext"],
                    "converted": False,
                }
                transferred += 1
            else:
                print("-> ERRO ao copiar [X]")

    # Save cache
    save_cache(source_dir, {"version": 1, "files": cache_new})

    # Rebuild database
    print("\n[*] Reconstruindo banco de dados do iPod...")
    all_tracks = []
    for key, val in cache_new.items():
        all_tracks.append({
            "ext": val["ext"],
            "ipod_filename": val["ipod_filename"],
            "artist": key.split("—")[0] if "—" in key else "Desconhecido",
        })

    rebuild_itunes_sd(ipod_mount, all_tracks)
    rebuild_pstate(ipod_mount)
    rebuild_stats(ipod_mount, len(all_tracks))
    rebuild_shuffle(ipod_mount, len(all_tracks))

    # Summary
    print(f"\n{'='*50}")
    print(f"[OK] Concluido! {transferred} arquivos transferidos")
    if ipod_info:
        new_free = ipod_info["free"] - sum(f["size"] for f in all_files)
        print(f"[DISK] Espaco livre: {format_size(max(0, new_free))}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
