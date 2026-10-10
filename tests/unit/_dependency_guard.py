import sys
import types
from unittest.mock import MagicMock


def ensure_dependencies_mocked():
    """mock missing external packages so unit tests pass in minimal environments."""
    if "pylast" not in sys.modules:
        try:
            import pylast  # noqa: F401
        except ImportError:
            pylast_mock = types.ModuleType("pylast")

            class MockWSError(Exception):
                def __init__(self, network=None, status="", details=""):
                    super().__init__(details or "")
                    self.network = network
                    self.status = str(status) if status is not None else ""
                    self.details = str(details) if details is not None else ""

            class MockNetworkError(Exception):
                pass

            class MockMalformedResponseError(Exception):
                pass

            class MockLastFMNetwork(MagicMock):
                pass

            class MockSessionKeyGenerator(MagicMock):
                pass

            pylast_mock.WSError = MockWSError
            pylast_mock.NetworkError = MockNetworkError
            pylast_mock.MalformedResponseError = MockMalformedResponseError
            pylast_mock.LastFMNetwork = MockLastFMNetwork
            pylast_mock.SessionKeyGenerator = MockSessionKeyGenerator
            pylast_mock.md5 = lambda x: MagicMock()

            sys.modules["pylast"] = pylast_mock

    if "dotenv" not in sys.modules:
        try:
            import dotenv  # noqa: F401
        except ImportError:
            dotenv_mock = types.ModuleType("dotenv")
            dotenv_mock.load_dotenv = MagicMock(return_value=True)
            dotenv_mock.set_key = MagicMock(return_value=True)
            sys.modules["dotenv"] = dotenv_mock

    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ImportError:
            requests_mock = types.ModuleType("requests")

            class MockRequestException(Exception):
                pass

            class MockHTTPError(MockRequestException):
                pass

            requests_mock.RequestException = MockRequestException
            requests_mock.HTTPError = MockHTTPError
            requests_mock.exceptions = types.ModuleType("requests.exceptions")
            requests_mock.exceptions.RequestException = MockRequestException
            requests_mock.exceptions.HTTPError = MockHTTPError
            requests_mock.get = MagicMock()
            requests_mock.post = MagicMock()

            sys.modules["requests"] = requests_mock
            sys.modules["requests.exceptions"] = requests_mock.exceptions


ensure_dependencies_mocked()
