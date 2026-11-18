"""Exceptions raised by aiointernxt.

The hierarchy separates what a caller has to do about an error:
- InternxtAuthError: ask the user to log in again (reauth).
- InternxtAccountError: the account cannot be used (plan, block); logging in again does not help.
- InternxtConnectionError: temporary, try again later.
- everything else: a concrete problem with this operation.
"""


class InternxtError(Exception):
    """Base class for all Internxt errors."""


class InternxtConnectionError(InternxtError):
    """The API could not be reached, timed out or answered with a server error."""


class InternxtRateLimitError(InternxtConnectionError):
    """Too many requests (HTTP 429)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InternxtAuthError(InternxtError):
    """Credentials or token were rejected; a new login is needed."""


class InternxtTwoFactorRequired(InternxtAuthError):
    """The account has 2FA enabled and no (or a wrong) TOTP code was given."""


class InternxtAccountError(InternxtError):
    """The account cannot be used for this, a new login does not change that."""


class InternxtPlanError(InternxtAccountError):
    """The account's plan does not allow third-party access (HTTP 402)."""


class InternxtAccountBlocked(InternxtAccountError):
    """The account is blocked after too many failed logins; only Internxt support can unblock."""


class InternxtQuotaError(InternxtError):
    """File larger than the plan allows, or storage full."""


class InternxtNotFoundError(InternxtError):
    """The requested file or folder does not exist."""


class InternxtConflictError(InternxtError):
    """A file with that name already exists in the folder (HTTP 409)."""


class InternxtIntegrityError(InternxtError):
    """Downloaded data does not match the stored hash."""


class InternxtStreamError(InternxtError):
    """The caller's source stream failed during an upload (see __cause__)."""
