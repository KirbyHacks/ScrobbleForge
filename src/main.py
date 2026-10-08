import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Support running directly as 'python src/main.py' or 'python -m src.main'
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import __version__
    from src.config import load_config
    from src.engine import ScrobblerEngine
    from src.lastfm_client import (
        LastFMClient,
        LastFMAuthError,
        LastFMAuthTimeoutError,
        LastFMAuthCancelledError,
        initiate_web_auth,
        poll_web_auth,
    )
    from src.queue_manager import QueueManager
    from src.quota_tracker import QuotaTracker
    from src.spotify_client import SpotifyClient, load_local_fallback_tracks
else:
    from . import __version__
    from .config import load_config
    from .engine import ScrobblerEngine
    from .lastfm_client import (
        LastFMClient,
        LastFMAuthError,
        LastFMAuthTimeoutError,
        LastFMAuthCancelledError,
        initiate_web_auth,
        poll_web_auth,
    )
    from .queue_manager import QueueManager
    from .quota_tracker import QuotaTracker
    from .spotify_client import SpotifyClient, load_local_fallback_tracks


DOCKER_COMPOSE_TEMPLATE = """services:
  scrobbler:
    image: ghcr.io/kirbyhacks/scrobbleforge:latest
    container_name: scrobbleforge-runner
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
"""


def cmd_init():
    """Generates starter docker-compose.yml and .env files."""
    out_dir = Path("/out") if Path("/out").is_dir() else Path(".")
    print(f"Generating ScrobbleForge starter files in: {out_dir.resolve()}")
    compose_path = out_dir / "docker-compose.yml"
    env_example_path = out_dir / ".env.example"
    env_path = out_dir / ".env"

    if not compose_path.exists():
        compose_path.write_text(DOCKER_COMPOSE_TEMPLATE, encoding="utf-8")
        print(f"  + Created: {compose_path.name}")
    else:
        print(f"  - Skipped: {compose_path.name} (already exists)")

    data_dir = out_dir / "data"
    if not data_dir.exists():
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            print(f"  + Created: {data_dir.name}/ (data storage directory)")
        except Exception:
            pass

    src_example = Path(".env.example")
    content = ""
    if src_example.is_file():
        content = src_example.read_text(encoding="utf-8")
    if not content:
        content = (
            "# Last.fm Credentials\n"
            "LASTFM_API_KEY=\n"
            "LASTFM_API_SECRET=\n"
            "LASTFM_USERNAME=\n"
            "LASTFM_SESSION_KEY=\n\n"
            "# Spotify Source\n"
            "SPOTIFY_PLAYLIST_URL=\n"
            "SCROBBLE_MODE=realistic\n"
        )

    if not env_example_path.exists():
        env_example_path.write_text(content, encoding="utf-8")
        print(f"  + Created: {env_example_path.name}")
    if not env_path.exists():
        env_path.write_text(content, encoding="utf-8")
        print(f"  + Created: {env_path.name}")
        print("\nNext steps:")
        print("1. Edit .env with your LASTFM_API_KEY, LASTFM_API_SECRET, and SPOTIFY_PLAYLIST_URL.")
        print("2. Run 'docker compose up -d' and open 'docker compose logs -f' to complete 1-click web authorization!")


def cmd_auth():
    """Runs interactive authorization helper."""
    import auth_helper
    auth_helper.main()


def setup_logging(log_level_str: str):
    """Sets up unified timestamped stdout logging."""
    level = getattr(logging, log_level_str.upper(), logging.INFO)
    log_format = "[%(asctime)s] [%(levelname)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt=date_format,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def main():
    args = sys.argv[1:]
    if "init" in args:
        cmd_init()
        return
    if "auth" in args:
        cmd_auth()
        return

    config = load_config()
    setup_logging(config.system.log_level)
    logger = logging.getLogger("scrobbler.main")

    logger.info(f"Initializing ScrobbleForge v{__version__}...")

    stop_event = threading.Event()

    def handle_shutdown_signal(signum, frame):
        try:
            sig_name = signal.Signals(signum).name
        except Exception:
            sig_name = str(signum)
        logger.info(f"Received shutdown signal ({sig_name}). Stopping gracefully...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)

    if not config.lastfm.api_key or not config.lastfm.api_secret:
        logger.error(
            "Missing LASTFM_API_KEY or LASTFM_API_SECRET.\n"
            "Please create an API account at https://www.last.fm/api/account/create and add them to your .env file."
        )
        sys.exit(1)

    # Web authorization handshake
    if not config.lastfm.session_key and not config.lastfm.password:
        logger.info("No LASTFM_SESSION_KEY found. Initiating one-time Last.fm web authorization...")
        try:
            skg, auth_url = initiate_web_auth(config.lastfm.api_key, config.lastfm.api_secret)
        except Exception as exc:
            logger.error(f"Failed to initialize Last.fm web authorization: {exc}")
            sys.exit(1)

        border = "=" * 70
        logger.info(border)
        logger.info("[ACTION REQUIRED] AUTHORIZE LAST.FM IN YOUR BROWSER")
        logger.info("Please open the following link and click 'Yes, allow access':")
        logger.info("")
        logger.info(f"  {auth_url}")
        logger.info("")
        logger.info("Waiting for browser approval (checking every 6s, 15m timeout)...")
        logger.info(border)

        try:
            session_key, username = poll_web_auth(
                skg=skg,
                auth_url=auth_url,
                stop_event=stop_event,
                poll_interval=6.0,
                timeout_seconds=900.0,
            )
        except LastFMAuthCancelledError:
            logger.info("Authorization cancelled by shutdown signal. Exiting.")
            sys.exit(0)
        except LastFMAuthTimeoutError as e:
            logger.error(f"{e}")
            sys.exit(1)
        except LastFMAuthError as e:
            logger.error(f"{e}")
            sys.exit(1)

        logger.info(border)
        logger.info(f"[SUCCESS] Authorized successfully as Last.fm user: '{username}'")
        logger.info(border)

        config.lastfm.session_key = session_key
        if not config.lastfm.username:
            config.lastfm.username = username

        config.system.data_dir.mkdir(parents=True, exist_ok=True)
        session_file = config.system.data_dir / "session.key"
        try:
            session_file.write_text(session_key + "\n", encoding="utf-8")
            try:
                os.chmod(session_file, 0o600)
            except Exception:
                pass
            logger.info(f"Persisted session key to {session_file}")
        except Exception as write_err:
            logger.warning(f"Could not persist session key to disk: {write_err}")

        env_file = Path(".env")
        if env_file.is_file():
            try:
                from dotenv import set_key
                set_key(str(env_file), "LASTFM_SESSION_KEY", session_key)
                if username and not os.getenv("LASTFM_USERNAME"):
                    set_key(str(env_file), "LASTFM_USERNAME", username)
                logger.info(f"Updated {env_file} with session key.")
            except Exception:
                pass

    config.system.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = config.system.data_dir / "state.db"
    tracker = QuotaTracker(db_path=db_path)
    tracker.prune_old_records(retention_days=14)

    tracks = []
    spotify_client = None

    if config.spotify.sources:
        logger.info(f"Resolving {len(config.spotify.sources)} configured playlist/album source(s)...")
        cache_file = config.system.data_dir / "tracks_cache.json"
        spotify_client = SpotifyClient(
            client_id=config.spotify.client_id,
            client_secret=config.spotify.client_secret,
            cache_path=cache_file,
            ttl_hours=config.spotify.refresh_interval_hours,
        )
        try:
            tracks = spotify_client.fetch_sources(config.spotify.sources, use_cache_if_available=True)
        except Exception as e:
            logger.error(f"Failed to fetch tracks from Spotify: {e}")

    # Fallback checks if Spotify yielded no tracks
    if not tracks:
        for fallback_candidate in [
            config.system.data_dir / "tracks.csv",
            config.system.data_dir / "tracks.json",
            config.system.data_dir / "tracks.txt",
            Path("tracks.csv"),
            Path("tracks.json"),
            Path("tracks.txt"),
        ]:
            if fallback_candidate.is_file():
                logger.info(f"Loading fallback tracks from {fallback_candidate}...")
                tracks = load_local_fallback_tracks(fallback_candidate)
                if tracks:
                    break

    if not tracks:
        logger.error(
            "No tracks available to scrobble!\n"
            "Please provide a valid SPOTIFY_PLAYLIST_URL in .env, "
            "or place a tracks.txt file in the directory formatted as 'Artist - Title' per line."
        )
        sys.exit(1)

    logger.info(f"Successfully loaded {len(tracks)} track(s) for scrobbling.")

    # Authenticate with Last.fm
    try:
        lastfm_client = LastFMClient(
            api_key=config.lastfm.api_key,
            api_secret=config.lastfm.api_secret,
            username=config.lastfm.username,
            session_key=config.lastfm.session_key,
            password=config.lastfm.password,
        )
    except LastFMAuthError as e:
        logger.error(f"Last.fm authentication error: {e}")
        sys.exit(1)

    # Setup queue manager
    queue = QueueManager(
        tracks=tracks,
        tracker=tracker,
        shuffle=config.engine.shuffle,
        loop=config.engine.loop,
    )

    # Launch engine
    engine = ScrobblerEngine(
        config=config,
        lastfm=lastfm_client,
        spotify=spotify_client,
        queue=queue,
        tracker=tracker,
        stop_event=stop_event,
    )

    engine.run()


if __name__ == "__main__":
    main()
