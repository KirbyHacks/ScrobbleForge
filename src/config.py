import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv


class ConfigError(ValueError):
    """raised when environment configuration fails validation."""
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


def _clean_credential(val: Optional[str]) -> Optional[str]:
    """filter out empty strings and standard template placeholders."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    low = s.lower()
    if "your_" in low or "placeholder" in low:
        return None
    return s


def _parse_source_references(raw: Optional[str]) -> List[str]:
    """parse comma-separated source references, trimming whitespace and preserving entry order and repeated references."""
    if raw is None or not str(raw).strip():
        return []
    items = str(raw).split(",")
    return [item.strip() for item in items if item.strip()]


@dataclass
class LastFMConfig:
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    username: Optional[str] = None
    session_key: Optional[str] = None
    password: Optional[str] = None


@dataclass
class SourceConfig:
    """provider-independent source configuration."""
    sources: List[str] = field(default_factory=list)
    refresh_interval_hours: float = 12.0
    provider_options: dict = field(default_factory=dict)


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
    sources: SourceConfig = field(default_factory=SourceConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    system: SystemConfig = field(default_factory=SystemConfig)

    def __post_init__(self):
        # backward compatibility: if legacy caller provided spotify.sources
        # and omitted generic sources, initialize sources from spotify.sources.
        if not self.sources.sources and self.spotify.sources:
            self.sources.sources = list(self.spotify.sources)

        # sync refresh interval from legacy spotify if generic was left at default
        if self.sources.refresh_interval_hours == 12.0 and self.spotify.refresh_interval_hours != 12.0:
            self.sources.refresh_interval_hours = self.spotify.refresh_interval_hours

    @classmethod
    def from_env(cls, env_path: Optional[str] = None) -> "AppConfig":
        """load and validate configuration from environment variables and optional .env file."""
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
        session_key = _clean_credential(os.getenv("LASTFM_SESSION_KEY"))
        if not session_key:
            session_file = data_dir / "session.key"
            if session_file.is_file():
                try:
                    content = session_file.read_text(encoding="utf-8").strip()
                    session_key = _clean_credential(content)
                except Exception:
                    pass

        lastfm_cfg = LastFMConfig(
            api_key=_clean_credential(os.getenv("LASTFM_API_KEY")),
            api_secret=_clean_credential(os.getenv("LASTFM_API_SECRET")),
            username=_clean_credential(os.getenv("LASTFM_USERNAME")),
            session_key=session_key,
            password=_clean_credential(os.getenv("LASTFM_PASSWORD")),
        )

        # refresh interval precedence:
        # SOURCES_REFRESH_INTERVAL_HOURS -> SPOTIFY_REFRESH_INTERVAL_HOURS -> default 12.0
        spotify_refresh_raw = os.getenv("SPOTIFY_REFRESH_INTERVAL_HOURS")
        sources_refresh_raw = os.getenv("SOURCES_REFRESH_INTERVAL_HOURS")

        spotify_refresh_hours = _parse_float(
            "SPOTIFY_REFRESH_INTERVAL_HOURS",
            spotify_refresh_raw,
            default=12.0,
            strictly_positive=True,
        )

        if sources_refresh_raw is not None and str(sources_refresh_raw).strip():
            sources_refresh_hours = _parse_float(
                "SOURCES_REFRESH_INTERVAL_HOURS",
                sources_refresh_raw,
                default=spotify_refresh_hours,
                strictly_positive=True,
            )
        else:
            sources_refresh_hours = spotify_refresh_hours

        # if legacy spotify refresh was omitted but generic was set, sync to spotify config
        if (spotify_refresh_raw is None or not str(spotify_refresh_raw).strip()) and (
            sources_refresh_raw is not None and str(sources_refresh_raw).strip()
        ):
            spotify_effective_refresh = sources_refresh_hours
        else:
            spotify_effective_refresh = spotify_refresh_hours

        # source references precedence:
        # 1. MUSIC_SOURCES (generic provider-independent source list)
        # 2. Legacy Spotify variables: SPOTIFY_TRACK_URL -> SPOTIFY_PLAYLIST_URL -> SPOTIFY_URL
        raw_generic = os.getenv("MUSIC_SOURCES")
        if raw_generic is not None and str(raw_generic).strip():
            sources = _parse_source_references(raw_generic)
            # generic source list is provider-independent; do not contaminate spotify.sources
            spotify_sources = []
        else:
            raw_legacy = (
                os.getenv("SPOTIFY_TRACK_URL")
                or os.getenv("SPOTIFY_PLAYLIST_URL")
                or os.getenv("SPOTIFY_URL", "")
            )
            sources = _parse_source_references(raw_legacy)
            # in legacy mode, sources are specifically Spotify sources
            spotify_sources = list(sources)

        spotify_cfg = SpotifyConfig(
            sources=spotify_sources,
            client_id=_clean_credential(os.getenv("SPOTIFY_CLIENT_ID")),
            client_secret=_clean_credential(os.getenv("SPOTIFY_CLIENT_SECRET")),
            refresh_interval_hours=spotify_effective_refresh,
        )

        sources_cfg = SourceConfig(
            sources=list(sources),
            refresh_interval_hours=sources_refresh_hours,
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
            inter_track_pause_min=pause_min,
            inter_track_pause_max=pause_max,
        )

        return cls(
            lastfm=lastfm_cfg,
            spotify=spotify_cfg,
            sources=sources_cfg,
            engine=engine_cfg,
            system=system_cfg,
        )


def load_config(env_path: Optional[str] = None) -> AppConfig:
    """load configuration from environment variables and .env file."""
    return AppConfig.from_env(env_path=env_path)
