import logging
import threading
import time
from typing import Optional, Tuple
import pylast
from .models import Track

logger = logging.getLogger("scrobbler.lastfm")


class LastFMAuthError(Exception):
    """Raised on authentication or session failure."""
    pass


class LastFMAuthTimeoutError(LastFMAuthError):
    """Raised when web authorization polling exceeds the hard timeout."""
    pass


class LastFMAuthCancelledError(LastFMAuthError):
    """Raised when web authorization is cancelled via shutdown signal."""
    pass


class LastFMRateLimitError(Exception):
    """Raised when Last.fm rate limits requests (code 29)."""
    pass


class LastFMTemporaryError(Exception):
    """Raised on transient network or server errors."""
    pass


def initiate_web_auth(api_key: str, api_secret: str) -> Tuple[pylast.SessionKeyGenerator, str]:
    """Initializes a Last.fm Web Authentication session and returns (generator, auth_url)."""
    network = pylast.LastFMNetwork(api_key=api_key.strip(), api_secret=api_secret.strip())
    skg = pylast.SessionKeyGenerator(network)
    auth_url = skg.get_web_auth_url()
    return skg, auth_url


def poll_web_auth(
    skg: pylast.SessionKeyGenerator,
    auth_url: str,
    stop_event: threading.Event,
    poll_interval: float = 6.0,
    timeout_seconds: float = 900.0,
) -> Tuple[str, str]:
    """
    Non-blocking poller for Last.fm web authorization.
    Handles Last.fm Error 14 ('This token has not been authorized') until user approves in browser.
    Respects stop_event for clean shutdown and enforces a hard timeout.
    Returns (session_key, username).
    """
    start_time = time.time()
    logger.info("Awaiting Last.fm browser approval...")

    while not stop_event.is_set():
        if time.time() - start_time >= timeout_seconds:
            raise LastFMAuthTimeoutError(
                f"Last.fm authorization timed out after {int(timeout_seconds / 60)} minutes without approval."
            )

        try:
            session_key, username = skg.get_web_auth_session_key_username(auth_url)
            if session_key and username:
                return str(session_key).strip(), str(username).strip()
        except pylast.WSError as exc:
            # Code 14: This token has not been authorized yet
            if str(exc.status) == "14" or "not been authorized" in str(exc.details).lower():
                pass
            else:
                raise LastFMAuthError(
                    f"Last.fm authorization rejected with code {exc.status}: {exc.details}"
                ) from exc
        except (pylast.NetworkError, pylast.MalformedResponseError) as exc:
            logger.warning(f"Transient network error while checking authorization: {exc}")
        except Exception as exc:
            raise LastFMAuthError(f"Unexpected error during authorization: {exc}") from exc

        # Wait using stop_event to allow instantaneous SIGINT/SIGTERM interruption
        if stop_event.wait(timeout=poll_interval):
            raise LastFMAuthCancelledError("Authorization cancelled by shutdown signal.")

    raise LastFMAuthCancelledError("Authorization cancelled by shutdown signal.")


class LastFMClient:
    """Wrapper for Last.fm API interactions using session key or password authentication."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        username: str,
        session_key: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.username = username.strip()
        self.session_key = session_key.strip() if session_key else None
        self.password = password.strip() if password else None
        self.network: Optional[pylast.LastFMNetwork] = None

        self._authenticate()

    def _authenticate(self):
        """Initializes LastFMNetwork session."""
        if not self.api_key or not self.api_secret:
            raise LastFMAuthError("Missing LASTFM_API_KEY or LASTFM_API_SECRET.")

        if self.session_key:
            logger.info("Authenticating with Last.fm via session key.")
            try:
                self.network = pylast.LastFMNetwork(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    session_key=self.session_key,
                )
            except Exception as e:
                raise LastFMAuthError(f"Failed to initialize session key: {e}") from e
        elif self.password and self.username:
            logger.info("Authenticating with Last.fm via password hash.")
            try:
                self.network = pylast.LastFMNetwork(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    username=self.username,
                    password_hash=pylast.md5(self.password),
                )
            except Exception as e:
                raise LastFMAuthError(f"Password authentication failed: {e}") from e
        else:
            raise LastFMAuthError("LASTFM_SESSION_KEY required. Run 'python auth_helper.py' to generate one.")

        try:
            user = self.network.get_authenticated_user()
            if user:
                logger.info(f"Verified Last.fm session for user: '{user.get_name()}'")
        except pylast.WSError as e:
            if "Invalid session" in str(e) or e.status == "9":
                raise LastFMAuthError(f"Session key invalid or expired: {e.details}") from e
            raise
        except Exception as e:
            logger.warning(f"Could not verify Last.fm user session at startup: {e}")

    def update_now_playing(self, track: Track) -> bool:
        """Sends track.updateNowPlaying to Last.fm."""
        if not self.network:
            return False

        try:
            self.network.update_now_playing(
                artist=track.artist,
                title=track.title,
                album=track.album if track.album else None,
                album_artist=track.album_artist if track.album_artist else None,
                track_number=track.track_number,
                duration=track.duration_sec,
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to update Now Playing: {e}")
            return False

    def scrobble(self, track: Track, timestamp: Optional[int] = None) -> bool:
        """Submits track.scrobble to Last.fm."""
        if not self.network:
            raise LastFMAuthError("Last.fm client is not authenticated.")

        ts = timestamp if timestamp is not None else int(time.time())

        try:
            self.network.scrobble(
                artist=track.artist,
                title=track.title,
                timestamp=ts,
                album=track.album if track.album else None,
                album_artist=track.album_artist if track.album_artist else None,
                track_number=track.track_number,
                duration=track.duration_sec,
            )
            return True
        except pylast.WSError as e:
            if str(e.status) == "29" or "Rate limit" in str(e.details):
                raise LastFMRateLimitError(f"Rate limit exceeded: {e.details}") from e
            if str(e.status) == "9" or "Invalid session" in str(e.details):
                raise LastFMAuthError(f"Session key expired: {e.details}") from e
            if str(e.status) in ("11", "16", "26"):
                raise LastFMTemporaryError(f"Server error ({e.status}): {e.details}") from e
            raise
        except (pylast.NetworkError, pylast.MalformedResponseError) as e:
            raise LastFMTemporaryError(f"Network error: {e}") from e
