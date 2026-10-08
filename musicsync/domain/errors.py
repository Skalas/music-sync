"""Domain-level exceptions shared across application boundaries."""

from __future__ import annotations


class PlatformOperationError(Exception):
    """Raised when an expected platform read/write operation cannot complete."""

