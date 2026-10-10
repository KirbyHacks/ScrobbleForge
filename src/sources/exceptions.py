"""exceptions for music source clients and providers."""


class SourceError(Exception):
    """base exception for all source client errors."""

    pass


class UnsupportedSourceError(SourceError):
    """raised when a source url or provider is not recognized or unsupported."""

    pass


class SourceAuthError(SourceError):
    """raised when authentication fails for a source provider."""

    pass


class SourceTemporaryError(SourceError):
    """raised when a transient network or provider failure occurs."""

    pass
