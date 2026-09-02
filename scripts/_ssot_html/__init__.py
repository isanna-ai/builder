"""Render a product SSOT directory into one self-contained HTML page."""

from .render import (
    DEFAULT_ORDER,
    RenderError,
    collect_documents,
    render_page,
)

__all__ = [
    "DEFAULT_ORDER",
    "RenderError",
    "collect_documents",
    "render_page",
]
