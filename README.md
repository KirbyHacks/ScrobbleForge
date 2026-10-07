# ScrobbleForge

A resilient, background-oriented Last.fm scrobbler designed for continuous, unattended execution. Ingests full Spotify playlists, complete albums, and single tracks (for obsession farming) without requiring Spotify Premium or developer credentials, featuring realistic playback simulation and anti-ban pacing up to Last.fm's daily limits.

---

## Why ScrobbleForge Instead of Other Scrobblers

Most open-source Last.fm auto-scrobblers share the same limitations:

| Feature / Capability | Other Scrobblers | ScrobbleForge |
|---|---|---|
| **Playlists, Albums & Single Tracks** | ❌ Hardcoded to single static track loops | ✅ Full playlists, complete albums, and single obsession tracks |
| **Free Spotify Accounts** | ❌ Blocked by Spotify Web API paywall | ✅ Zero-credential public resolution (No Premium needed) |
| **Anti-Ban Pacing** | ❌ Naive real-time flooding (overlapping tracks) | ✅ Non-overlapping sequential historical backdating |
| **Rolling 24h Quota Guard** | ❌ None or midnight resets (causes HTTP 429) | ✅ Exact rolling 24-hour SQLite ledger with auto-pause/resume |
| **Two-Factor Authentication (2FA)** | ❌ Broken with deprecated password hashing | ✅ Permanent session key authorization (`auth_helper.py`) |
| **Production Docker Readiness** | ❌ Blocking `time.sleep()` hangs on `SIGTERM` | ✅ Non-root (`1000:1000`), persistent WAL SQLite, signal-safe |

---

## Operating Modes

### 1. `realistic` (Humanly Possible)
- Simulates real-time listening behavior.
- Broadcasts `track.updateNowPlaying` to Last.fm immediately so profiles show active playback.
- Submits `track.scrobble` at the 50% / 4-minute mark per official Last.fm rules.
- Adds randomized inter-track pauses (1 to 4 seconds).
- Yields ~300 to 500 scrobbles per day, identical to genuine listening.

### 2. `max_limit` (2,800 Safe Daily Pacing)
- Paced at ~32-second intervals to safely approach Last.fm's ~2,800 daily limit.
- Maintains a continuous, non-overlapping historical playback timeline to prevent account flags.
- Enforces an internal safety ceiling of 2,750 scrobbles per rolling 24 hours, automatically throttling until older timestamps age out.

### 3. `custom_interval`
- Submits scrobbles at a fixed user-defined interval in seconds while still enforcing rolling 24-hour safety caps.

---

## Quick Start (Docker)

### Option A: Using Pre-Built Image (No Git Clone Required)

1. Create a project folder and generate the starter configuration files:

```bash
# On Linux / macOS:
docker run --rm -v "${PWD}:/out" ghcr.io/kirbyhacks/scrobbleforge:latest init

# On Windows PowerShell:
docker run --rm -v "${PWD}:/out" ghcr.io/kirbyhacks/scrobbleforge:latest init
```

*(Alternatively, download `docker-compose.yml` and `.env.example` directly from GitHub).*

2. Edit `.env` with your Last.fm API Key, API Secret, and Spotify URL:

```dotenv
LASTFM_API_KEY=your_lastfm_api_key
LASTFM_API_SECRET=your_lastfm_api_secret
SPOTIFY_PLAYLIST_URL=https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
```

3. Start the container:

```bash
docker compose up -d
docker compose logs -f
```

4. **1-Click Zero-Restart Authorization**:
   On first launch, ScrobbleForge displays an authorization link in the container logs:
   ```text
   ======================================================================
   [ACTION REQUIRED] AUTHORIZE LAST.FM IN YOUR BROWSER
   Please open the following link and click "Yes, allow access":

     https://www.last.fm/api/auth/?api_key=...&token=...

   Waiting for browser approval (checking every 6s, 15m timeout)...
   ======================================================================
   ```
   Open the link in your browser and click **"Yes, allow access"**. ScrobbleForge automatically detects your approval, saves the permanent key to `data/session.key`, and immediately begins scrobbling — **no restarts needed**.

---

### Option B: Build from Source

```bash
git clone https://github.com/KirbyHacks/ScrobbleForge.git
cd "ScrobbleForge"
cp .env.example .env
# Edit .env with your credentials, then:
docker compose up -d --build
docker compose logs -f
```

---

## Configuration Reference

Settings can be specified in `.env` or passed directly as Docker environment variables.

| Variable | Default | Description |
|---|---|---|
| `LASTFM_API_KEY` | *(required)* | Last.fm API Key (from [last.fm/api/account/create](https://www.last.fm/api/account/create)) |
| `LASTFM_API_SECRET` | *(required)* | Last.fm Shared Secret |
| `LASTFM_USERNAME` | *(optional)* | Last.fm Account Username (auto-detected on authorization) |
| `LASTFM_SESSION_KEY` | *(auto-generated)* | Permanent session key. If omitted, ScrobbleForge logs a 1-click web approval link and auto-saves to `data/session.key` |
| `SPOTIFY_PLAYLIST_URL` | *(required)* | Spotify playlist, album, or single track URL. Comma-separate for multiple sources |
| `SCROBBLE_MODE` | `realistic` | Pacing mode: `realistic`, `max_limit`, or `custom_interval` |
| `CUSTOM_INTERVAL_SECONDS` | `60` | Delay in seconds when `SCROBBLE_MODE=custom_interval` |
| `MAX_DAILY_SCROBBLES` | `2750` | Rolling 24-hour safety ceiling (Last.fm hard limit: 2,800) |
| `SHUFFLE` | `true` | Randomizes the playback queue on each pass |
| `LOOP` | `true` | Repeats queue infinitely for continuous background scrobbling |
| `UPDATE_NOW_PLAYING` | `true` | Broadcasts "Now Playing" in `realistic` mode |
| `INTER_TRACK_PAUSE_MIN` | `1.0` | Minimum pause between tracks in seconds |
| `INTER_TRACK_PAUSE_MAX` | `4.0` | Maximum pause between tracks in seconds |
| `SPOTIFY_REFRESH_INTERVAL_HOURS` | `12` | Hours between automatic source playlist refreshes |
| `DATA_DIR` | `./data` | Directory where SQLite state and cache are persisted |
| `LOG_LEVEL` | `INFO` | Console logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Standalone Execution (Without Docker)

```bash
# Setup environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Authorize
python auth_helper.py

# Launch
python -m src.main
```

*(Alternatively: `python src/main.py`)*

---

## Offline and Local Fallback Sources

If a Spotify URL is unavailable or local metadata is preferred, ScrobbleForge automatically looks for fallback files in the working directory or `./data`:

- `tracks.txt`: Line-delimited entries in `Artist - Title` format.
- `tracks.csv`: Exportify or TuneMyMusic CSV exports with standard track headers.
- `tracks.json`: JSON list containing objects with `title`, `artist`, and `duration_ms`.

---

## Test Suite

ScrobbleForge includes a comprehensive unit test suite covering duration models, rolling quota mathematics, queue management, and non-overlapping timeline pacing:

```bash
python tests/test_suite.py
```

---

## What's Next (Roadmap)

ScrobbleForge is designed with a decoupled source ingestion architecture to support future streaming provider wrappers and monitoring tools:

- **YouTube Music Wrapper**: Native zero-auth resolution for `music.youtube.com` public playlists, albums, and video tracks with zero Google API keys or YouTube Premium required.
- **Apple Music & Deezer Wrappers**: Direct metadata extraction from public Apple Music and Deezer web links to scrobble catalog releases.
- **Lightweight Web UI & Status Dashboard**: Optional minimal local dashboard and Prometheus metrics endpoint displaying real-time playback state, rolling 24h scrobble quotas, and queue progression.
- **Multi-Account Concurrent Scrobbling**: Relay playback streams concurrently across multiple Last.fm or Libre.fm accounts.

---

## License

MIT License. Free for personal and educational use.
