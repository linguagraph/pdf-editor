"""Signing failures, with messages written for the person at the keyboard."""

from __future__ import annotations


class SigningError(Exception):
    """Signing didn't happen; ``str(error)`` says why and what to do."""


class SigningRefused(SigningError):
    """The document or certificate doesn't allow this signature."""


class WrongPassword(SigningError):
    """A PFX or document password was wrong."""


class WrongPin(SigningError):
    """The token rejected the PIN. The caller must never retry on its own: tokens lock."""


class PinLocked(SigningError):
    """The token's PIN is locked; only the token's own tools can unlock it."""


class TokenUnavailable(SigningError):
    """The token or smart card isn't there (or stopped answering)."""


class TimestampFailed(SigningError):
    """The timestamp server couldn't be reached or refused the request."""
