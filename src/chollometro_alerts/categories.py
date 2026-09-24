"""Stable, provider-shaped category values and their matching helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def normalize_category(value: str | None) -> str:
    """Case/accent/punctuation-insensitive key used by every category surface."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


@dataclass(frozen=True)
class CategoryRef:
    id: str | None = None
    slug: str | None = None
    name: str | None = None
    parent_id: str | None = None
    parent_slug: str | None = None
    parent_name: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in (
                self.id,
                self.slug,
                self.name,
                self.parent_id,
                self.parent_slug,
                self.parent_name,
            )
            if key
        )

    def matches(self, requested: str) -> bool:
        expected = normalize_category(requested)
        return bool(expected) and any(
            normalize_category(key) == expected for key in self.keys
        )


def category_matches(categories: tuple[CategoryRef, ...], requested: str) -> bool:
    return any(category.matches(requested) for category in categories)
