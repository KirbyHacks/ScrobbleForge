import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when environment configuration fails validation."""
    pass


def _parse_bool(name: str, val: Optional[str], default: bool) -> bool:
    if val is None or not str(val).strip():
        return default
    v = str(val).strip().lower()
    if v in ("true", "1", "yes", "on", "t", "y"):
        return True
    if v in ("false", "0", "no", "off", "f", "n"):
        return False
    raise ConfigError(
        f"Invalid boolean value for {name}='{val}'. Expected one of: true, false, 1, 0, yes, no."
    )


def _parse_int(
    name: str,
    val: Optional[str],
    default: int,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> int:
    if val is None or not str(val).strip():
        return default
    raw = str(val).strip()
    try:
        parsed = int(raw)
    except ValueError:
        if min_val == 1 and max_val is not None:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a positive integer <= {max_val}.")
        elif min_val == 1:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a positive integer.")
        raise ConfigError(f"Invalid {name}='{val}'. Must be an integer.")

    if min_val is not None and parsed < min_val:
        if min_val == 1 and max_val is not None:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a positive integer <= {max_val}.")
        elif min_val == 1:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a positive integer.")
        raise ConfigError(f"Invalid {name}='{val}'. Must be at least {min_val}.")

    if max_val is not None and parsed > max_val:
        if min_val == 1:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a positive integer <= {max_val}.")
        raise ConfigError(f"Invalid {name}='{val}'. Must be at most {max_val}.")

    return parsed


def _parse_float(
    name: str,
    val: Optional[str],
    default: float,
    min_val: Optional[float] = None,
    strictly_positive: bool = False,
) -> float:
    if val is None or not str(val).strip():
        return default
    raw = str(val).strip()
    try:
        parsed = float(raw)
    except ValueError:
        if strictly_positive:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a positive float.")
        elif min_val == 0.0:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a non-negative float.")
        raise ConfigError(f"Invalid {name}='{val}'. Must be a valid number.")

    if strictly_positive and parsed <= 0.0:
        raise ConfigError(f"Invalid {name}='{val}'. Must be a positive float.")

    if min_val is not None and parsed < min_val:
        if min_val == 0.0:
            raise ConfigError(f"Invalid {name}='{val}'. Must be a non-negative float.")
        raise ConfigError(f"Invalid {name}='{val}'. Must be at least {min_val}.")

    return parsed


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
    refresh_interval_hours: float = 12.0


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

    @classmethod
    def from_env(cls, env_path: Optional[str] = None) -> "AppConfig":
        """Loads and validates configuration from environment variables and optional .env file."""
        if env_path:
            load_dotenv(dotenv_path=env_path)
        else:
            for p in [".env", Path(__file__).resolve().parent.parent / ".env"]:
                if Path(p).exists():
                    load_dotenv(dotenv_path=p)
                    break
            else:
                load_dotenv()

        # system settings
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        system_cfg = SystemConfig(
            data_dir=data_dir,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )

        # last.fm credentials
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

        # spotify configuration
        raw_sources = os.getenv("SPOTIFY_TRACK_URL") or os.getenv("SPOTIFY_PLAYLIST_URL") or os.getenv("SPOTIFY_URL", "")
        sources = [s.strip() for s in raw_sources.split(",") if s.strip()]
        refresh_hours = _parse_float(
            "SPOTIFY_REFRESH_INTERVAL_HOURS",
            os.getenv("SPOTIFY_REFRESH_INTERVAL_HOURS"),
            default=12.0,
            strictly_positive=True,
        )
        spotify_cfg = SpotifyConfig(
            sources=sources,
            client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip() or None,
            client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip() or None,
            refresh_interval_hours=refresh_hours,
        )

        # engine settings validation
        raw_mode = os.getenv("SCROBBLE_MODE")
        if raw_mode is None or not raw_mode.strip():
            mode = "realistic"
        else:
            mode = raw_mode.strip().lower()
            if mode not in ("realistic", "max_limit", "custom_interval"):
                raise ConfigError(
                    f"Invalid SCROBBLE_MODE='{raw_mode}'. Expected one of: realistic, max_limit, custom_interval."
                )

        custom_interval = _parse_int(
            "CUSTOM_INTERVAL_SECONDS",
            os.getenv("CUSTOM_INTERVAL_SECONDS"),
            default=60,
            min_val=1,
        )
        max_daily = _parse_int(
            "MAX_DAILY_SCROBBLES",
            os.getenv("MAX_DAILY_SCROBBLES"),
            default=2750,
            min_val=1,
            max_val=2800,
        )
        min_duration = _parse_int(
            "MIN_TRACK_DURATION_SECONDS",
            os.getenv("MIN_TRACK_DURATION_SECONDS"),
            default=30,
            min_val=1,
        )
        pause_min = _parse_float(
            "INTER_TRACK_PAUSE_MIN",
            os.getenv("INTER_TRACK_PAUSE_MIN"),
            default=1.0,
            min_val=0.0,
        )
        pause_max = _parse_float(
            "INTER_TRACK_PAUSE_MAX",
            os.getenv("INTER_TRACK_PAUSE_MAX"),
            default=4.0,
            min_val=0.0,
        )
        if pause_min > pause_max:
            raise ConfigError(
                f"Invalid pause range: INTER_TRACK_PAUSE_MIN ({pause_min}) cannot be greater than INTER_TRACK_PAUSE_MAX ({pause_max})."
            )

        shuffle = _parse_bool("SHUFFLE", os.getenv("SHUFFLE"), default=True)
        loop = _parse_bool("LOOP", os.getenv("LOOP"), default=True)
        update_now_playing = _parse_bool("UPDATE_NOW_PLAYING", os.getenv("UPDATE_NOW_PLAYING"), default=True)

        engine_cfg = EngineConfig(
            mode=mode,
            custom_interval_seconds=custom_interval,
            shuffle=shuffle,
            loop=loop,
            update_now_playing=update_now_playing,
            max_daily_scrobbles=max_daily,
            min_track_duration_seconds=min_duration,
            inter_track_pause_min=pause_min,
            inter_track_pause_max=pause_max,
        )

        return cls(
            lastfm=lastfm_cfg,
            spotify=spotify_cfg,
            engine=engine_cfg,
            system=system_cfg,
        )


def load_config(env_path: Optional[str] = None) -> AppConfig:
    """Loads configuration from environment variables and .env file."""
    return AppConfig.from_env(env_path=env_path)
