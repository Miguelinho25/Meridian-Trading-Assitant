"""HTTP edge. Validates, delegates, serialises — owns no business logic."""

from __future__ import annotations

from meridian_api.app import app, create_app

__all__ = ["app", "create_app"]
