from __future__ import annotations


def escape_alembic_url(url: str) -> str:
    """Escape ConfigParser interpolation while preserving the effective SQLAlchemy URL."""
    return url.replace("%", "%%")
