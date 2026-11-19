"""Read-only project briefings backed by transactional local state."""

from .store import ConflictError, Store

__all__ = ["ConflictError", "Store"]
