"""Exceptions raised by nordic-balancing."""


class NordicBalancingError(Exception):
    """Base class for every error this library raises."""


class SourceError(NordicBalancingError):
    """A data source returned something this library cannot interpret."""


class RateLimitError(NordicBalancingError):
    """A data source kept rate-limiting the client after every allowed retry."""

    def __init__(self, source: str, retry_after: float | None) -> None:
        self.source = source
        self.retry_after = retry_after
        hint = f"; retry after {retry_after:g} s" if retry_after is not None else ""
        super().__init__(f"{source} rate limit exceeded{hint}")
