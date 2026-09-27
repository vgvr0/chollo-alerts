import json
from importlib.resources import files

SUPPORTED_LANGUAGES = ("es", "en")
DEFAULT_LANGUAGE = "es"
LANGUAGE_NAMES = {"es": "Español", "en": "English"}


class Translator:
    """Load locale files once and provide safe key-based interpolation."""

    def __init__(self, package="chollometro_alerts.i18n.locales"):
        self._package = package
        self._catalogs = {
            language: self._load(language) for language in SUPPORTED_LANGUAGES
        }

    def _load(self, language):
        try:
            return json.loads(
                files(self._package)
                .joinpath(f"{language}.json")
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def normalize_language(language):
        value = str(language or DEFAULT_LANGUAGE).strip().casefold().replace("_", "-")
        return value.split("-", 1)[0]

    def t(self, key, locale=DEFAULT_LANGUAGE, **values):
        requested = self.normalize_language(locale)
        text = self._catalogs.get(requested, {}).get(key)
        if text is None:
            text = self._catalogs.get(DEFAULT_LANGUAGE, {}).get(key)
        if text is None:
            return key
        try:
            if "|" in text and "count" in values:
                text = text.split("|", 1)[0 if values["count"] == 1 else 1]
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return text

    def is_supported(self, language):
        return self.normalize_language(language) in SUPPORTED_LANGUAGES
