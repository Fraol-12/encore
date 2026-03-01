import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


class TransientAPIError(RuntimeError):
    """Retryable upstream error (429/5xx)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class UnauthorizedAPIError(RuntimeError):
    """Raised when upstream returns 401 and account should be deactivated."""


class ForbiddenAPIError(RuntimeError):
    """Raised when upstream returns 403 and caller lacks permission/scope."""


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After header (seconds or HTTP date)."""
    if not value:
        return None

    stripped = value.strip()
    try:
        numeric = float(stripped)
        return max(0.0, numeric)
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(stripped)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delta = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (ValueError, TypeError):
        return None


def retry_with_backoff(
    operation_name: str,
    func: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_sleep_seconds: float = 30.0,
) -> T:
    """Retry operation for transient API failures with exponential backoff."""
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except TransientAPIError as exc:
            last_error = exc
            if attempt >= max_attempts:
                break

            delay = exc.retry_after if exc.retry_after is not None else base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Transient API failure during %s (attempt %s/%s). Retrying in %.2fs",
                operation_name,
                attempt,
                max_attempts,
                delay,
            )
            time.sleep(min(delay, max_sleep_seconds))

    raise RuntimeError(f"{operation_name} failed after {max_attempts} attempts") from last_error
