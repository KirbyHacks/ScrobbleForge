import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv


def _to_bool(val: any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes", "on", "y")


@dataclass
class LastFMConfig:
    api_key: str
    api_secret: str
    username: str
    session_key: Optional[str] = None
    password: Optional[str] = None


@dataclass
class SpotifyConfig:
    sources: List[str] = field(default_factory=list)
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    refresh_interval_hours: int = 12


@dataclass
class EngineConfig:
    mode: str = "realistic"  # realistic, max_limit, custom_interval
    custom_interval_seconds: int = 60
    shuffle: bool = True
    loop: bool = True
    update_now_playing: bool = True
    max_daily_scrobbles: int = 2750
    min_track_duration_seconds: int = 30
    inter_track_pause_min: float = 1.0
    inter_track_pause_max: float = 4.0


@dataclass
class SystemConfig:
    data_dir: Path = Path("./data")
    log_level: str = "INFO"


@dataclass
class AppConfig:
    lastfm: LastFMConfig
    spotify: SpotifyConfig
    engine: EngineConfig = field(default_factory=EngineConfig)
    system: SystemConfig = field(default_factory=SystemConfig)


def load_config(env_path: Optional[str] = None) -> AppConfig:
    """Loads configuration from environment variables and .env file."""
    if env_path:
        load_dotenv(dotenv_path=env_path)
    else:
        for p in [".env", Path(__file__).resolve().parent.parent / ".env"]:
            if Path(p).exists():
                load_dotenv(dotenv_path=p)
                break
        else:
            load_dotenv()

    # System settings
    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    system_cfg = SystemConfig(
        data_dir=data_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )

    # Last.fm credentials
    session_key = os.getenv("LASTFM_SESSION_KEY", "").strip() or None
    if not session_key:
        session_file = data_dir / "session.key"
        if session_file.is_file():
            try:
                content = session_file.read_text(encoding="utf-8").strip()
                if content:
                    session_key = content
            except Exception:
                pass

    lastfm_cfg = LastFMConfig(
        api_key=os.getenv("LASTFM_API_KEY", "").strip(),
        api_secret=os.getenv("LASTFM_API_SECRET", "").strip(),
        username=os.getenv("LASTFM_USERNAME", "").strip(),
        session_key=session_key,
        password=os.getenv("LASTFM_PASSWORD", "").strip() or None,
    )

    # Spotify configuration (supports playlist, album, and track URLs)
    raw_sources = os.getenv("SPOTIFY_TRACK_URL") or os.getenv("SPOTIFY_PLAYLIST_URL") or os.getenv("SPOTIFY_URL", "")
    sources = [s.strip() for s in raw_sources.split(",") if s.strip()]
    spotify_cfg = SpotifyConfig(
        sources=sources,
        client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip() or None,
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip() or None,
        refresh_interval_hours=max(1, int(os.getenv("SPOTIFY_REFRESH_INTERVAL_HOURS", "12"))),
    )

    # Engine settings
    mode = os.getenv("SCROBBLE_MODE", "realistic").strip().lower()
    if mode not in ("realistic", "max_limit", "custom_interval"):
        mode = "realistic"

    engine_cfg = EngineConfig(
        mode=mode,
        custom_interval_seconds=max(5, int(os.getenv("CUSTOM_INTERVAL_SECONDS", "60"))),
        shuffle=_to_bool(os.getenv("SHUFFLE", "true"), default=True),
        loop=_to_bool(os.getenv("LOOP", "true"), default=True),
        update_now_playing=_to_bool(os.getenv("UPDATE_NOW_PLAYING", "true"), default=True),
        max_daily_scrobbles=min(2800, max(1, int(os.getenv("MAX_DAILY_SCROBBLES", "2750")))),
        min_track_duration_seconds=max(1, int(os.getenv("MIN_TRACK_DURATION_SECONDS", "30"))),
        inter_track_pause_min=max(0.0, float(os.getenv("INTER_TRACK_PAUSE_MIN", "1.0"))),
        inter_track_pause_max=max(
            float(os.getenv("INTER_TRACK_PAUSE_MIN", "1.0")),
            float(os.getenv("INTER_TRACK_PAUSE_MAX", "4.0")),
        ),
    )



    return AppConfig(
        lastfm=lastfm_cfg,
        spotify=spotify_cfg,
        engine=engine_cfg,
        system=system_cfg,
    )
