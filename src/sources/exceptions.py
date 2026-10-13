"""exceptions for music source clients and providers."""
from typing import Optional, Tuple


class SourceError(Exception):
    """base exception for all source client errors."""

    pass


class UnsupportedSourceError(SourceError):
    """raised when a source url or provider is not recognized or unsupported."""

    pass


class SourceAuthError(SourceError):
    """raised when authentication fails for a source provider."""

    def __init__(
        self,
        message: object = "",
        status_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.status_code = status_code


class SourceTemporaryError(SourceError):
    """raised when a transient network or provider failure occurs."""

    def __init__(
        self,
        message: object = "",
        status_code: Optional[int] = None,
        retry_after: Optional[int] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def extract_http_metadata(exc: Exception) -> Tuple[Optional[int], Optional[int]]:
    """extract structured (status_code, retry_after) from an exception or its causes."""
    status_code: Optional[int] = None
    retry_after: Optional[int] = None

    curr: Optional[BaseException] = exc
    visited = set()
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))

        # check direct status_code
        if status_code is None:
            candidate = getattr(curr, "status_code", None)
            if isinstance(candidate, int):
                status_code = candidate

        # check response object
        resp = getattr(curr, "response", None)
        if resp is not None:
            if status_code is None:
                candidate = getattr(resp, "status_code", None)
                if isinstance(candidate, int):
                    status_code = candidate

            if retry_after is None:
                headers = getattr(resp, "headers", None)
                if isinstance(headers, dict) or hasattr(headers, "get"):
                    raw_ra = headers.get("Retry-After") or headers.get("retry-after")
                    if raw_ra is not None:
                        try:
                            retry_after = int(str(raw_ra).strip())
                        except (ValueError, TypeError):
                            pass

        # check direct retry_after attribute
        if retry_after is None:
            candidate_ra = getattr(curr, "retry_after", None)
            if isinstance(candidate_ra, int):
                retry_after = candidate_ra

        # advance through cause and context chain
        curr = getattr(curr, "__cause__", None) or getattr(curr, "__context__", None)

    return status_code, retry_after
