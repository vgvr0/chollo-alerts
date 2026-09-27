"""Small, explicit translation layer for user-facing messages."""

from .translator import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    Translator,
)

__all__ = ["DEFAULT_LANGUAGE", "LANGUAGE_NAMES", "SUPPORTED_LANGUAGES", "Translator"]
