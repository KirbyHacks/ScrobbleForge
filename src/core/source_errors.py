"""errors shared by the core and music providers."""
from typing import Optional


class SourceError(Exception):
    pass


class UnsupportedSourceError(SourceError):
    pass


class SourceAuthError(SourceError):
    def __init__(self, message: object = "", status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class SourceTemporaryError(SourceError):
    def __init__(self, message: object = "", status_code: Optional[int] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
