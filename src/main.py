import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Support running directly as 'python src/main.py' or 'python -m src.main'
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import load_config
    from src.engine import ScrobblerEngine
    from src.lastfm_client import LastFMClient, LastFMAuthError
    from src.queue_manager import QueueManager
    from src.quota_tracker import QuotaTracker
    from src.spotify_client import SpotifyClient, load_local_fallback_tracks
else:
    from .config import load_config
    from .engine import ScrobblerEngine
    from .lastfm_client import LastFMClient, LastFMAuthError
    from .queue_manager import QueueManager
    from .quota_tracker import QuotaTracker
    from .spotify_client import SpotifyClient, load_local_fallback_tracks


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
    config = load_config()
    setup_logging(config.system.log_level)
    logger = logging.getLogger("scrobbler.main")

    logger.info("Initializing ScrobbleForge v1.0.0...")

    # Validate essential Last.fm credentials
    if not config.lastfm.api_key or not config.lastfm.api_secret:
        logger.error(
            "Missing LASTFM_API_KEY or LASTFM_API_SECRET.\n"
            "Please create an API account at https://www.last.fm/api/account/create and add them to your .env file."
        )
        sys.exit(1)

    if not config.lastfm.session_key and not config.lastfm.password:
        logger.error(
            "Missing LASTFM_SESSION_KEY.\n"
            "Please run 'python auth_helper.py' to authorize your Last.fm account and obtain a session key."
        )
        sys.exit(1)

    # Prepare data directory & quota tracker
    config.system.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = config.system.data_dir / "state.db"
    tracker = QuotaTracker(db_path=db_path)
    tracker.prune_old_records(retention_days=14)

    # Ingest tracks (Spotify Free/API or local fallback)
    tracks = []
    spotify_client = None

    if config.spotify.sources:
        logger.info(f"Resolving {len(config.spotify.sources)} configured playlist/album source(s)...")
        cache_file = config.system.data_dir / "tracks_cache.json"
        spotify_client = SpotifyClient(
            client_id=config.spotify.client_id,
            client_secret=config.spotify.client_secret,
            cache_path=cache_file,
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

    # Setup queue manager & stop event
    queue = QueueManager(
        tracks=tracks,
        tracker=tracker,
        shuffle=config.engine.shuffle,
        loop=config.engine.loop,
    )

    stop_event = threading.Event()

    def handle_shutdown_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"Received shutdown signal ({signum}: {sig_name}). Stopping gracefully...")
        stop_event.set()

    # Register OS signals for graceful Docker / CLI termination
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)

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
