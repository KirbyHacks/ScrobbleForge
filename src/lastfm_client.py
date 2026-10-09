import logging
import threading
import time
from typing import Optional, Tuple
from xml.dom import minidom
import pylast
from .models import Track

logger = logging.getLogger("scrobbler.lastfm")

LASTFM_MIN_SCROBBLE_DURATION_SEC = 30


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
        username: Optional[str] = None,
        session_key: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.username = (username or "").strip() or None
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

        self._verify_session_validity()

    def _verify_session_validity(self):
        """Verifies session validity fail-fast by testing get_authenticated_user()."""
        try:
            user = self.network.get_authenticated_user()
            if user is None:
                raise LastFMAuthError("Invalid session: No authenticated user returned.")
            try:
                auto_name = user.get_name()
                if not self.username and auto_name:
                    self.username = auto_name
                logger.info(f"Verified Last.fm session for user: '{self.username or auto_name or 'authenticated'}'")
            except pylast.WSError:
                raise
            except (pylast.NetworkError, pylast.MalformedResponseError, ConnectionError, TimeoutError, OSError):
                raise
            except Exception as e:
                logger.warning(f"Could not retrieve Last.fm username: {e}")
        except pylast.WSError as e:
            status = str(getattr(e, "status", ""))
            details = str(getattr(e, "details", str(e)))
            if status in ("4", "9", "10") or "invalid session" in str(e).lower() or "invalid session" in details.lower():
                raise LastFMAuthError(f"Last.fm authentication failed ({status or 'unknown'}): {details}") from e
            if status in ("11", "16", "26", "29"):
                raise LastFMTemporaryError(f"Last.fm service temporary error ({status}): {details}") from e
            # Do not disguise protocol/configuration errors as connectivity failures.
            raise
        except (pylast.NetworkError, pylast.MalformedResponseError, ConnectionError, TimeoutError, OSError) as e:
            raise LastFMTemporaryError(f"Network error during session verification: {e}") from e

    def update_now_playing(self, track: Track) -> bool:
        """Sends track.updateNowPlaying to Last.fm."""
        if not self.network:
            return False

        scrobble_duration = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, track.duration_sec)

        try:
            self.network.update_now_playing(
                artist=track.artist,
                title=track.title,
                album=track.album if track.album else None,
                album_artist=track.album_artist if track.album_artist else None,
                track_number=track.track_number,
                duration=scrobble_duration,
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to update Now Playing: {e}")
            return False

    def scrobble(self, track: Track, timestamp: Optional[int] = None) -> bool:
        """Submits track.scrobble to Last.fm via pylast._Request and inspects response XML."""
        if not self.network:
            raise LastFMAuthError("Last.fm client is not authenticated.")

        ts = timestamp if timestamp is not None else int(time.time())
        scrobble_duration = max(LASTFM_MIN_SCROBBLE_DURATION_SEC, track.duration_sec)

        params = {
            "artist[0]": track.artist,
            "track[0]": track.title,
            "timestamp[0]": str(ts),
            "duration[0]": str(scrobble_duration),
        }
        if track.album:
            params["album[0]"] = track.album
        if track.album_artist:
            params["albumArtist[0]"] = track.album_artist
        if track.track_number:
            params["trackNumber[0]"] = str(track.track_number)

        try:
            doc = pylast._Request(self.network, "track.scrobble", params).execute()
            if doc is None:
                raise LastFMTemporaryError("Empty XML response received from Last.fm.")

            # Check <scrobbles ignored="N">
            scrobbles_nodes = doc.getElementsByTagName("scrobbles")
            if not scrobbles_nodes:
                raise LastFMTemporaryError("[SCROBBLE] Malformed response: missing <scrobbles> tag")

            ignored_count = scrobbles_nodes[0].getAttribute("ignored")
            if ignored_count and ignored_count != "0":
                msg_nodes = doc.getElementsByTagName("ignoredMessage")
                code = msg_nodes[0].getAttribute("code") if msg_nodes else "unknown"
                message = (
                    msg_nodes[0].firstChild.nodeValue
                    if (msg_nodes and msg_nodes[0].firstChild and hasattr(msg_nodes[0].firstChild, "nodeValue"))
                    else ""
                )
                logger.warning(f"[IGNORED SCROBBLE] code {code}: {message}")
                return False

            # Check <ignoredMessage code="...">
            msg_nodes = doc.getElementsByTagName("ignoredMessage")
            if msg_nodes:
                node = msg_nodes[0]
                code = node.getAttribute("code")
                if code and code != "0":
                    message = (
                        node.firstChild.nodeValue
                        if (node.firstChild and hasattr(node.firstChild, "nodeValue"))
                        else ""
                    )
                    logger.warning(f"[IGNORED SCROBBLE] code {code}: {message}")
                    return False

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
